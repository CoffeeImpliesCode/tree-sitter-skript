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
| Block comment | `#\| text \|#` | `block_comment` |
| Datum comment | `#;FORM` | `datum_comment` |
| Shebang | `#! text` | `shebang` |
| Definition | `(def NAME VALUE)` | `def` |
| Definition with parameters | `(defn NAME (ARGS) BODY...)` | `defn` |
| Lambda | `(fn (ARGS) BODY...)` | `fn` |
| Call | `(EXPR EXPR...)` | `call` |
| Dotted (improper) list | `(EXPR... . EXPR)` | `call`, dotted form carries a `tail:` field |
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

`#t`, `#f` and `nil` are ordinary identifiers to the Skript reader, which
resolves those three exact spellings to values. The grammar lifts them to
nodes so they can be highlighted; keyword extraction keeps `#true`, `nil?`
and `nilx` as single identifiers, matching the reader.

File types: `.cpt`, `.pt`. Query scope: `source.skript`.

## Deliberate departures from the reader

The Skript reader has no special forms: `def`, `defn` and `fn` are ordinary
symbols, so `(fn rest rest)` is just a list. The grammar gives them named
nodes for editor value, and keeps every head shape the reader accepts so it
never rejects a program the reader reads.

Two known gaps remain, both deliberate:

- **`{}` curly infix.** The grammar parses `{a + b}` as `infix_expr`, per
  Skript's own `docs/notation-design.md`. The reader still answers
  `error.Unimplemented` for a bare `{`, so the grammar is ahead of it.
- **Nested block comments.** `#| a |#` is a `block_comment`, but the reader
  nests (`#| a #| b |# c |#`, `src/Token.zig:216-231`) and tree-sitter cannot
  express unbounded nesting. An external scanner could, but a token listed in
  `extras` is only consulted at byte offset 0, so it would read the same text
  as a comment in one position and as three identifiers everywhere else. The
  flat token is wrong only for the nested spelling.

Two places where the grammar is deliberately more permissive than the reader,
because tree-sitter cannot express the reader's rules:

- A number must be followed by whitespace or a delimiter (`src/Token.zig:364`),
  so the reader rejects `42abc` and `1_000`. The grammar reads them as a number
  followed by an identifier.
- `#!` is a shebang only at byte offset 0. A file whose first line is blank and
  whose second starts with `#!` is read by the grammar as a shebang.

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