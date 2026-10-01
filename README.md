# tree-sitter-skript

Tree-sitter grammar for [Skript](https://skript.sh), the typed Lisp that
compiles to a register VM.

The parser is generated C. The repository ships bindings for C, Go, Node,
Python, Rust, Swift, and Zig.

## Language surface

The grammar covers the Skript reader, not the compiler. It parses source text
into reader forms.

| Form | Syntax | Node |
| --- | --- | --- |
| Comment | `; text` | `line_comment` |
| Shebang | `#! text` | `shebang` |
| Definition | `(def NAME VALUE)` | `def` |
| Definition with parameters | `(defn NAME (ARGS) BODY...)` | `defn` |
| Lambda | `(fn (ARGS) BODY...)` | `fn` |
| Call | `(EXPR EXPR...)` | `call` |
| Tensor literal | `[EXPR...]` | `array_lit` |
| Curly infix | `{EXPR op EXPR}` | `infix_expr` |
| Map literal | `:{ :KEY VALUE... }` | `map_lit` |
| Quote | `' EXPR` | `quote_lit` |
| Identifier | `@rgba`, `ffi:make-func` | `identifier` |
| Number | `#x1f`, `1.5e3`, `2i`, `inf.0` | `num_lit` |
| String | `"text"` | `string_lit` |
| Keyword | `:NAME` | `kwd_lit` |
| Boolean | `#t`, `#f` | `bool_lit` |
| Nil | `nil` | `nil_lit` |

File types: `.cpt`, `.pt`. Query scope: `source.skript`.

## Queries

`queries/` holds the editor queries.

- `highlights.scm` maps head symbols and atom tokens to capture names.
- `outline.scm` captures definition names.
- `brackets.scm` captures the `()`, `[]`, and `{}` pairs.
- `injections.scm` is a placeholder. It holds no captures yet.

## Development

Use [devenv](https://devenv.sh). It provides `tree-sitter` and Zig 0.16.

```sh
direnv allow
```

Regenerate the parser after you edit `grammar.js`.

```sh
tree-sitter generate
```

Run the corpus tests.

```sh
tree-sitter test
```

Build the C library and run the Zig binding test. Zig 0.16 is the minimum
version.

```sh
zig build
zig build test
```

## Bindings

The Makefile and CMake build the C library. Cargo, Go, npm, PyPI, and Swift
package managers build the other bindings. Each directory under `bindings/`
holds one.

## License

MIT. See [LICENSE](LICENSE).
