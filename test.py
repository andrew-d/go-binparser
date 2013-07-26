#!/usr/bin/env python
from __future__ import print_function

import sys
import os
import tempfile
import subprocess


def generate_code(input_file):
    out = subprocess.check_output([
        './generate.py',
        input_file
    ])
    return out


temp_dir = tempfile.mkdtemp()
test_dir = os.path.join(os.path.abspath(os.path.dirname(__file__)), 'tests')

for fname in os.listdir(test_dir):
    name, ext = os.path.splitext(fname)
    if ext.lower() != '.test':
        continue

    print("========== %s" % (name,))
    path = os.path.join(test_dir, fname)

    # Generate the code.
    code = generate_code(path)

    # Append the test stub to the generated code.
