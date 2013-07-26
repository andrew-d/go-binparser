#!/usr/bin/env python
"""
The general purpose of this program is to take a structured representation of
a data structure (pun not intended), and output Go code that will parse this
structure into a similar in-memory structure.

- A structure consists of a name, and some number of fields.  Optional
  attributes include: endianness (defaults to little-endian), ...
- A field consists of a name, an offset in the parent structure, and then a
  storage specifier that determines what to read.  Valid storage specifiers
  are the integral types:
      - [u]int[8|16|32|64]
      - uintptr (pointer-sized type, differs between endian-ness)

  And the helpful extensions:
      - bitfield (which then consists of other sub-fields)
      - arrays of integral types

  A field can also specify the endianness manually.  If not overridden, it will
  default to that of the parent structure.

Note that this project is NOT designed to be a general-purpose binary parsing
library, a la Python's Construct.  Since I might add other language targets in
the future, this library is designed to be the lowest common denominator of
binary parsing - taking a single structure and its associated fields, and
reading them into an in-memory representation in the host language.

Examples, in YAML format:

---
name: Test1
fields:
    - name: foo
      offset: 0
      type: uint8
    - name: bar
      offset: 1
      type: int16
    - name: bits
      offset: 3
      type: bitfield.uint8      # Anything after the '.' is taken to be a
                                # storage specifier, and the generator will
                                # validate that no more than the specified
                                # number of bits will be used.  Note that
                                # signed/unsigned doesn't matter here, except
                                # that we might include an option to read the
                                # whole value of the bitfield.
      fields:
          - name: flag1         # Default: 1 bit
          - name: not_a_flag    # Override number of bits
            size: 2
    - name: an_array
      offset: 4
      type: uint16[2]           # 2 uint16s, back-to-back
"""

# TODO list (in order):
#   - Standardize on direction for bitfield (start from left/right?)
#   - Check on arrays of bitvectors

from __future__ import print_function

import os
import re
import sys
import json
import textwrap
from collections import namedtuple


def set_bits(num):
    val = 0
    ctr = 1
    for i in range(num):
        val |= ctr
        ctr = ctr * 2

    return val


def count_bits(num):
    return bin(num).count("1")


def test_set_bits():
    assert set_bits(0) == 0
    assert set_bits(1) == 1
    assert set_bits(2) == 3
    assert set_bits(32) == 0xFFFFFFFF
    assert set_bits(64) == 0xFFFFFFFFFFFFFFFF


_NAME_RE = re.compile(r'^(u)?int(8|16|32|64)$')

def validate_integral_type(name):
    return (name == 'uintptr' or
            _NAME_RE.match(name) is not None)


def test_validate_integral_type():
    assert validate_integral_type('uint8') == True
    assert validate_integral_type('uint88') == False
    assert validate_integral_type('uint') == False
    assert validate_integral_type('int16') == True
    assert validate_integral_type('uint32') == True
    assert validate_integral_type('uintptr') == True
    assert validate_integral_type('intptr') == False


_ARRAY_RE = re.compile(r'(.*?)\[(\d)+\]')

SIZES = {
    'int8': 1,
    'uint8': 1,
    'int16': 2,
    'uint16': 2,
    'int32': 4,
    'uint32': 4,
    'int64': 8,
    'uint64': 8,
}


Field = namedtuple('Field', [
    'name',
    'offset',
    'ty',
    'sub_type',
    'bit_fields',
    'array_num',
    'endian',
])


BitField = namedtuple('BitField', [
    'name',
    'size',
])


Struct = namedtuple('Struct', ['name', 'fields'])


class Parser(object):
    def __init__(self):
        self.structs = []

    def add(self, spec):
        for struct in spec:
            self.add_one(struct)

    def add_one(self, struct):
        s_name = struct['name']
        def_endian = struct.get('endian', 'little')

        # Collect each field's data, with certain defaults.
        fields = []
        for field in struct['fields']:
            # Required
            name       = field['name']
            offset     = int(field['offset'])
            ty         = field['type']
            sub_type   = None
            bit_fields = None
            array_num  = None
            endian     = field.get('endian', def_endian)

            # If the type is 'bitfield', we need the bit type and bit fields.
            if ty.startswith('bitfield'):
                ty, sub_type = ty.split('.', 1)

                # Our subfields default to 1 bit in size.
                bit_fields = [
                    BitField(x['name'], x.get('size', 1))
                        for x in field['bit_fields']
                ]

            # If the type matches the array datatype, we extract the number
            # of elements, and properly set the sub-type.
            match = _ARRAY_RE.match(ty)
            if match is not None:
                sub_type, array_num = match.groups()
                array_num = int(array_num)
                ty = 'array'

            # Create our field.
            field = Field(name, offset, ty, sub_type, bit_fields, array_num,
                          endian)

            # Validate it.
            self._check_field(field)
            if field.ty == 'bitfield':
                self._check_bitfield(s_name, field)

            fields.append(field)

        # Create the final struct.
        s = Struct(s_name, fields)
        self.structs.append(s)

    def _check_field(self, field):
        # Validate the name.
        if field.ty in ['array', 'bitfield']:
            check = field.sub_type
        else:
            check = field.ty

        valid = validate_integral_type(check)
        if not valid:
            raise Exception('Invalid type given for field "%s": %s' % (
                name, check))

    def _check_bitfield(self, struct_name, field):
        """
        Check a single bitfield to ensure that it doesn't use more bits
        than the size it was declared with.
        """
        # Bitfields can't be pointer-sized.
        if field.sub_type == 'uintptr':
            raise Exception('Bitfields can\'t be uintptr (field "%s" in "%s")'
                            % (field.name, struct_name))

        num_bits = sum(x.size for x in field.bit_fields)
        max_bits = SIZES[field.sub_type] * 8
        if num_bits > max_bits:
            raise Exception(
                'Too many bits in field "%s" of structure "%s" '
                '- used: %d, max: %d' % (field.name, struct_name,
                                         num_bits, max_bits))


def _ds(s):
    return textwrap.dedent(s).replace('\r', '').split('\n')


class Generator(object):
    PRELUDE = _ds("""
        import (
        \t"encoding/binary"
        \t"errors"
        \t"fmt"
        \t"io"
        )
        """)

    def __init__(self, pkg_name, add_binfield=True):
        self.pkg_name = pkg_name
        self.add_binfield = add_binfield
        self.defs = []

    def generate(self):
        data = []

        data.append('package %s' % (self.pkg_name,))
        data.append('')
        data.extend(self.PRELUDE)
        data.append('')
        data.extend(self.defs)

        return '\n'.join(data)

    def add_from_parser(self, parser):
        for struct in parser.structs:
            self.add_one(struct)

    def add_one(self, struct):
        # Create the output name for the structure.
        output_name = struct.name[0].capitalize() + struct.name[1:]

        # Create the structure definition in the order it's given (i.e. do it
        # before sorting).
        defn = [
            'type %s struct {' % (output_name,)
        ]
        for field in struct.fields:
            # Depending on the type...
            if field.ty == 'bitfield':
                for sub in field.bit_fields:
                    defn.append('\t%s %s' % (self.output_field_name(sub),
                                             field.sub_type))

            elif field.ty == 'array':
                defn.append('\t%s [%d]%s' % (self.output_field_name(field),
                                             field.array_num,
                                             field.sub_type))

            else:
                # Add the field.
                defn.append('\t%s %s' % (self.output_field_name(field),
                                         field.ty))

        defn.append('}')

        # Sort the array by the offset in the structure.
        s_fields = sorted(struct.fields, key=lambda f: f[1])

        # Our general algorithm is as follows:
        # Given the array of fields, sorted by the offset in the structure,
        # we start emitting reading code.  For each field, we do the following:
        #   - If the previous field's end wasn't at the start of this field, we
        #     emit reading code that reads and discards n bytes, where n is the
        #     difference between the previous end and the current start.
        #   - If the current field is not a bitfield, just read to the proper
        #     final field.
        #   - Otherwise, read to a temporary and decode.

        # Used for obtaining temporary variables.
        ctr = [0]       # Lack of 'nonlocal' in Python 2
        def get_temp():
            tmp = "temp%d" % (ctr[0],)
            ctr[0] += 1
            return tmp

        code = [
            'var output %s' % (output_name,),
            'var err error',
            '',
        ]
        last_name = '*beginning of structure*'
        last_end = 0

        for field in s_fields:
            diff = field.offset - last_end
            if diff != 0:
                tmp = get_temp()
                code.append('// Reading %d byte(s) of slack between "%s" and '
                            '"%s"' % (diff, last_name, field.name))
                code.append('var %s [%d]byte' % (tmp, diff))
                code.append('binary.Read(input, binary.LittleEndian, '
                            '&%s)' % (tmp,))
                code.append('')

            code.append('// Reading into field "%s"' % (field.name,))

            # If not a bitfield...
            if field.ty == 'bitfield':
                field_name = get_temp()
                code.append('var %s %s' % (field_name, field.sub_type))
            else:
                field_name = 'output.' + self.output_field_name(field)

            err_msg = "Error reading field '%s' of structure: %%s" % (
                field.name)

            if field.endian == 'big':
                endian = 'binary.BigEndian'
            else:
                endian = 'binary.LittleEndian'

            code.append('err = binary.Read(input, %s, &%s)' % (
                endian, field_name
            ))
            code.append('if err != nil {')
            code.append('\treturn nil, '
                        'errors.New(fmt.Sprintf("%s", err))' % (err_msg,))
            code.append('}')
            code.append('')

            if field.ty == 'bitfield':
                # We need to unpack from the temporary name to the real one.
                size = SIZES[field.sub_type]

                # TODO: standardize on direction
                offset = 0
                mask = set_bits(size)
                for sub in field.bit_fields:
                    # For each field, we mask out the offset to offset + size
                    # bits, and then invert.
                    curr_mask = set_bits(sub.size) << offset
                    code.append('// Bit field "%s": offset %d, size %d' % (
                        sub.name, offset, sub.size,
                    ))

                    code.append('output.%s = (%s & 0x%X) >> %d' % (
                        self.output_field_name(sub),
                        field_name,
                        curr_mask,
                        offset,
                    ))

                    offset += sub.size

                code.append('')
                last_end = field.offset + size

            elif field.ty == 'array':
                last_end = (field.offset +
                            SIZES[field.sub_type] * field.array_num)

            else:
                last_end = field.offset + SIZES[field.ty]

            last_name = field.name

        code.append('// Total size of structure: %d bytes' % (last_end,))
        code.append('return &output, nil')

        # Add the definitions.
        self.defs.extend(defn)
        self.defs.append('')

        # Write the code (indented and everything!)
        self.defs.append('func Parse%s(input io.Reader) (*%s, error) {' % (
            output_name, output_name
        ))
        self.defs.extend('\t' + x for x in code)
        self.defs.append('}')

    def output_field_name(self, field):
        return field.name.title()


def main():
    if len(sys.argv) < 2:
        print("Usage: %s input.json\n" % (os.path.basename(sys.argv[0]),),
              file=sys.stderr)
        return 1

    d = json.load(open(sys.argv[1], 'rb'))
    name, _ = os.path.basename(sys.argv[1]).split('.', 1)
    parser = Parser()
    parser.add(d)
    gen = Generator(name)
    gen.add_from_parser(parser)

    print(gen.generate())

if __name__ == "__main__":
    try:
        r = main()
        sys.exit(r or 0)
    except KeyboardInterrupt:
        pass
