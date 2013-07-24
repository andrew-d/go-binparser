#!/usr/bin/env python

from __future__ import print_function

import os
import re
import sys
import json
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
    'type',
    'sub_type',
    'bit_fields',
    'array_num',
])

BitField = namedtuple('BitField', [
    'name',
    'size',
])


"""
The general meaning of this program is to take a structured representation of
a data structure (pun not intended), and output code for a specific language to
parse this data structure.  Currently, Google Go is the main language target.

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
library, a la Python's Construct.  Since I plan on adding other language
targets in the future, this library is designed to be the lowest common
denominator of binary parsing - taking a single structure and its associated
fields, and reading them into an in-memory representation in the host language.

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

"""


class Generator(object):
    PRELUDE = [
        'import (',
        '\t"encoding/binary"',
        '\t"errors"',
        '\t"fmt"',
        '\t"io"',
        ')',
    ]

    def __init__(self, pkg_name):
        self.pkg_name = pkg_name
        self.defs = []

    def generate(self):
        data = []

        data.append('package %s' % (self.pkg_name,))
        data.append('')
        data.extend(self.PRELUDE)
        data.append('')
        data.extend(self.defs)

        return '\n'.join(data)

    def add(self, spec):
        for struct in spec:
            self.add_one(struct)

    def add_one(self, struct):
        s_name = struct['name']

        # Collect a tuple of each field's data, with certain defaults.
        fields = []
        for field in struct['fields']:
            # Required
            name       = field['name']
            offset     = int(field['offset'])
            type       = field['type']
            sub_type   = None
            bit_fields = None
            array_num  = None

            # If the type is 'bitfield', we need the bit type and bit fields.
            if type.startswith('bitfield'):
                type, sub_type = type.split('.', 1)

                # Get all sub-fields.
                bit_fields = [
                    BitField(x['name'], x.get('size', 1))
                        for x in field['bit_fields']
                ]

                # Check this bitfield for consistency.
                num_bits = sum(x[1] for x in bit_fields)
                max_bits = SIZES[sub_type] * 8
                if num_bits > max_bits:
                    raise Exception(
                        'Too many bits in field "%s" of structure "%s" '
                        '- used: %d, max: %d' % (name, s_name,
                                                 num_bits, max_bits))

            # If the type matches the array datatype, we extract the number
            # of elements, and properly set the sub-type.
            match = _ARRAY_RE.match(type)
            if match is not None:
                sub_type, array_num = match.groups()
                array_num = int(array_num)
                type = 'array'

            fields.append(Field(name, offset, type, sub_type, bit_fields,
                                array_num))

        # Create the structure definition in the order it's given (i.e. do it
        # before sorting).
        defn = [
            'type %s struct {' % (s_name.title(),)
        ]
        for field in fields:
            # Depending on the type...
            if field.type == 'bitfield':
                for sub in field.bit_fields:
                    defn.append('\t%s %s' % (sub.name.title(), field.sub_type))

            elif field.type == 'array':
                defn.append('\t%s [%d]%s' % (field.name.title(),
                                             field.array_num,
                                             field.sub_type))

            else:
                # Add the field.
                defn.append('\t%s %s' % (field.name.title(), field.type))

        defn.append('}')

        # Sort the array by the offset in the structure.
        fields.sort(key=lambda f: f[1])

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
            'var output %s' % (s_name.title(),),
            'var err error',
            '',
        ]
        last_name = '*beginning of structure*'
        last_end = 0

        for field in fields:
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
            if field.type == 'bitfield':
                field_name = get_temp()
                code.append('var %s %s' % (field_name, field.sub_type))
            else:
                field_name = 'output.' + field.name.title()

            err_msg = "Error reading field '%s' of structure: %%s" % (
                field.name)

            # TODO: fix me!
            endian = 'binary.LittleEndian'
            code.append('err = binary.Read(input, %s, &%s)' % (
                endian, field_name
            ))
            code.append('if err != nil {')
            code.append('\treturn nil, '
                        'errors.New(fmt.Sprintf("%s", err))' % (err_msg,))
            code.append('}')
            code.append('')

            if field.type == 'bitfield':
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
                        sub.name.title(),
                        field_name,
                        curr_mask,
                        offset,
                    ))

                    offset += sub.size

                code.append('')
                last_end = field.offset + size

            elif field.type == 'array':
                last_end = (field.offset +
                            SIZES[field.sub_type] * field.array_num)

            else:
                last_end = field.offset + SIZES[field.type]

            last_name = field.name

        code.append('// Total size of structure: %d bytes' % (last_end,))
        code.append('return &output, nil')

        # Add the definitions.
        self.defs.extend(defn)
        self.defs.append('')

        # Write the code (indented and everything!)
        self.defs.append('func Parse%s(input io.Reader) (*%s, error) {' % (
            s_name.title(), s_name.title()
        ))
        self.defs.extend('\t' + x for x in code)
        self.defs.append('}')




def main():
    if len(sys.argv) < 2:
        print("Usage: %s input.json\n" % (os.path.basename(sys.argv[0]),),
              file=sys.stderr)
        return 1

    d = json.load(open(sys.argv[1], 'rb'))
    name, _ = os.path.basename(sys.argv[1]).split('.', 1)
    gen = Generator(name)
    gen.add(d)

    print(gen.generate())

if __name__ == "__main__":
    try:
        r = main()
        sys.exit(r or 0)
    except KeyboardInterrupt:
        pass
