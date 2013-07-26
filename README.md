# go-binparser

This project is a really simple binary parsing code generator for Google Go (a.k.a. Golang).  The aim is to help with parsing binary blobs, something that Go isn't great for, due to its inability to handle things like bitfields or structure alignment.

For more information, see the [docstring](https://github.com/andrew-d/go-binparser/blob/master/generate.py) in the generation script, which explains what's possible with this library.

For more concrete examples, check out the [tests](https://github.com/andrew-d/go-binparser/tree/master/tests) - specifically, the `.test` files, which specify a structure.

I aim to add an example of parsing a non-trivial structure eventually.

# License

MIT
