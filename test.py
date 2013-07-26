#!/usr/bin/env python
from __future__ import print_function

import os
import sys
import json
import tempfile
import subprocess

from generate import Parser, Generator


temp_dir = tempfile.mkdtemp()
test_dir = os.path.join(os.path.abspath(os.path.dirname(__file__)), 'tests')

for fname in os.listdir(test_dir):
    name, ext = os.path.splitext(fname)
    if ext.lower() != '.test':
        continue

    print("========== %s" % (name,))
    path = os.path.join(test_dir, fname)

    # Load this test file.
    d = json.load(open(path, 'rb'))

    # Load the test data.
    with open(os.path.join(test_dir, name + '.data'), 'rb') as f:
        test_data = f.read()

    # Load the expected output.
    with open(os.path.join(test_dir, name + '.out'), 'rb') as f:
        test_output = f.read().strip()

    # Parse the code.
    parser = Parser()
    parser.add(d)

    # Generate test code.
    gen = Generator('main')
    gen.add_from_parser(parser)
    generated_code = gen.generate()

    # Create a test stub that just dumps each struct field to stdout.
    struct = parser.structs[0]
    lines = [
        '',
        "func main() {",
    ]

    # Encode as a byte array for Go.
    hd = '\ttest_data := []byte{' + ', '.join(hex(ord(x)) for x in test_data) + '}'
    lines.extend([
        hd,
        '\tbuf := bytes.NewBuffer(test_data)',
        '\ttest, err := Parse%s(buf)' % (struct.name,),
        "\tif err != nil {",
        "\t\tpanic(err)",
        "\t}",
    ])

    for field in struct.fields:
        # Handle bitfields.
        if field.ty == 'bitfield':
            for sub in field.bit_fields:
                field_name = gen.output_field_name(sub)
                lines.append('\tfmt.Printf("%s,%%s\\n", test.%s)' % (
                    field_name, field_name))

        else:
            field_name = gen.output_field_name(field)
            lines.append('\tfmt.Printf("%s,%%s\\n", test.%s)' % (
                field_name, field_name))

    lines.append("}")

    # Append the test stub to the generated code.
    test_code = generated_code + '\n' + '\n'.join(lines)

    # We need another import!
    first, rest = test_code.split('\n', 1)
    test_code = first + '\n\nimport "bytes"' + rest

    # Save the file.
    test_file = os.path.join(temp_dir, name + ".go")
    with open(test_file, 'wb') as f:
        f.write(test_code)

    # Run the command.
    output = subprocess.check_output(['go', 'run', test_file]).strip()

    # Validate the command's output matches.
    if output != test_output:
        one = '\n'.join('\t' + x for x in output.split('\n'))
        two = '\n'.join('\t' + x for x in test_output.split('\n'))
        print('ERROR: output does not match!\nActual:\n%s\n\nExpected:\n%s' %
              (one, two), file=sys.stderr)
