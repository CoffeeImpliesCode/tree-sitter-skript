# tree-sitter-skript

Tree-sitter grammar for [Skript](https://skript.sh), the typed Lisp that
compiles to a register VM.

The parser is generated C, and `src/scanner.c` is required, not optional:
`grammar.js` declares `block_comment` and `datum_comment` as external tokens,
so the generated parser calls into that scanner to produce them. Every build
surface compiles it — the Makefile, CMake, `binding.gyp`, `setup.py`,
`build.zig`, `Package.swift`, `bindings/rust/build.rs` and the cgo preamble in
`bindings/go/binding.go` each list `src/scanner.c` beside `src/parser.c`, and
none of them has an optional path. A build that leaves the file out links a
parser that cannot produce the two comment forms.

The repository ships bindings for C, Go, Node, Python, Rust, Swift, and Zig.

## Language surface

The grammar covers the Skript reader, not the compiler. It parses source text
into reader forms.

| Form | Syntax | Node |
| --- | --- | --- |
| Comment | `; text` | `line_comment` |
| Block comment | `#\| text \|#` | `block_comment` |
| Datum comment | `#;FORM` | `datum_comment`, an extra leaf over the marker and the discarded form |
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

## Comments

`line_comment` is a token rule. `block_comment` and `datum_comment` are
external tokens listed in `extras`, and that is what puts them at **every
lexical token boundary**: tree-sitter consults extras before each token, which
is exactly where Skript's `lex` re-dispatches after skipping comments. A
comment therefore sits between any two tokens, at any depth, in any collection:

```skript
#| a #| b |# c |# 42          ; one nested block_comment, then a number
[1 #| a #| b |# c |# 2 3]     ; ditto inside a tensor
:{ :k #| a #| b |# c |# 1 }   ; ditto between a key and its value
#| a |#|# b |#                ; closer and opener do not merge
```

Strings and identifier bodies are opaque to the scanner, because each is a
single token the scanner is never consulted inside: `"#| a #| b |# c |#"` is one
`string_lit` and `foo#|bar|#baz` is one `identifier`. An identifier body ends at
whitespace or one of `()[]{};"` only, so `;` ends it: `a#;b c` is the identifier
`a#` followed by a `line_comment`.

An unterminated `#|` at end of input owns everything after it. That is
deliberate editor recovery — a file being typed is not a program yet — and it
is one of the departures listed below.

The scanner is stateless: both comment forms are decided by the input alone,
so `create` returns NULL and `serialize` writes nothing. Collection closers use
inline storage and spill to a heap buffer only beyond 16 open levels.

### Datum comments

A `datum_comment` is a childless `extra` node. It owns the `#;` marker plus
**exactly one form the reader actually reads and discards**, and no more:

| Source | Node |
| --- | --- |
| `#;1 2 3` | one `datum_comment` over `#;1`; `2` and `3` stay live |
| `#;#;1 2 3` | one `datum_comment` over `#;#;1 2` |
| `#;#;#;1 2 3 4` | one `datum_comment` over `#;#;#;1 2 3` |
| `#;'1 2` | the quote belongs to the discarded value; `2` stays live |
| `'#;1 2` | the live `2` is what gets quoted |
| `#;(a #;(b) c) 3` | a marker inside a discarded collection is opaque |
| `#;(1 2\n3` | an unterminated discarded list runs to end of input |
| `#;"unfinished` | an unterminated discarded string stays comment-styled through end of input |
| `#;` | owns only itself |
| `(+ 1 #;[2 3) 4` | the mismatched closer ends the value; `4` stays live |

Incomplete discarded forms use editor recovery. The reader rejects them, so
the differential verifier cannot confirm their full spans.

Because it is an `extra`, a datum comment never occupies a value slot:

```skript
(def a #;1 2)      ; the def binds 2
:{ :k #;1 2 }      ; the map keeps 2
(f #;1 2 3)        ; the call passes 2 and 3
```

The discarded body is a comment, not code. `highlights.scm` captures
`(datum_comment) @comment.block`, and because the node is childless, nothing
inside the discarded form is reachable — a discarded `(def x 1)` produces no
outline item and no bracket pair.

That is a consumer-visible CST cutover, taken on purpose. The earlier
`datum_comment` was structured: it carried the discarded form as children, so
those bytes were readable as parsed code. It is now a childless extra over the
marker and the value — the discarded children are gone, and the node no longer
sits in a form's value slot. There is no shim, alias or compatibility node.

## Deliberate departures from the reader

The Skript reader has no special forms: `def`, `defn` and `fn` are ordinary
symbols, so `(fn rest rest)` is just a list. The grammar gives them named nodes
for editor value, and their rules narrow what those heads take. A list the
reader reads is not thereby a list this grammar parses: `(def 1)` puts a
`num_lit` where the `def` rule wants an identifier, and no other head rule can
match that token, so the reader's list comes back as a grammar error. The
grammar makes no claim to accept everything the reader accepts.

`test/verify.py` cannot settle that question either. It is a lexer oracle: it
lexes with Skript's own `Token.zig` and parses with the generated parser, but it
never runs `Reader.zig`, so whether the reader's parser accepts a construct is
outside anything the harness can decide. ERROR nodes that fall outside an
unbalanced-`{` region are reported as `grammar_error_outside_braces` —
informational, never fatal, never a waiver, and carrying the note that a
lexer-only oracle cannot say whether the reader accepts the construct. The
structured `def`/`defn`/`fn` shapes are where those show up.

The remaining departures are narrow, and the harness classifies each one only
from evidence drawn from the two oracles:

- **Number terminator.** A number must be followed by whitespace, a grouping
  delimiter or end of input (`skript/src/Token.zig:364-369`), so the reader
  rejects `42abc` and `1_000`. The grammar reads them as a number followed by
  an identifier.
- **Shebang.** `#!` is a shebang only at byte offset 0
  (`Token.zig:190-195`); elsewhere the reader lexes `#!…` as one identifier. A
  grammar token cannot be anchored to offset 0, so `#!` at the start of any
  top-level line is a `shebang` here. Mid-line (`a #!b c`) stays identifiers.
- **Curly infix.** `{a + b}` parses as `infix_expr`, per Skript's own
  `docs/notation-design.md`. The reader answers `error.Unimplemented` for a
  bare `{` (`Reader.zig:408`), so the grammar is ahead of it there.
- **Unterminated block comment at end of input.** An unmatched `#|` owns the
  rest of the input so the editor keeps working. The reader fails with
  `error.UnexpectedToken` (`Token.zig:216-231`).

## Queries

`queries/` holds the editor queries.

- `highlights.scm` maps head symbols and atom tokens to capture names.
- `outline.scm` captures definition names, including the legacy arglist-first
  `defn` shape.
- `brackets.scm` captures the `()`, `[]`, and `{}` pairs.
- `injections.scm` holds no captures: Skript embeds no other language.

## Verification

`test/verify.py` compares two independent oracles and substitutes neither for
the other. The lexer oracle compiles a byte-for-byte copy of the live
`<skript>/src/Token.zig` and fails unless the staged copy hashes identical to
the file on disk. The parser oracle compiles the checked-in `src/parser.c`
plus `src/scanner.c` against an installed tree-sitter runtime, and refuses to
run if the generated parser's ABI falls outside that runtime's supported
range. The one reader-side rule the harness re-derives is the discarded
value's span: it recomputes that from the oracle's token kinds — the shape of
`Reader.zig`'s `readFormNode` — never from source text and never from the
parser's own claim, and checks that it starts on a `#;` token and ends on a
token edge.

Prerequisites: the Python standard library, a C compiler, Zig 0.16, a Skript
checkout with an unmodified `src/Token.zig`, and tree-sitter core 0.25 or newer
as both headers and library. Pass `--runtime-include` (a directory containing
`tree_sitter/api.h`) and `--runtime-library` (the `libtree-sitter` file) when
auto-discovery cannot find them.

```sh
python3 test/verify.py differential --skript ../skript --work-dir PARENT
python3 test/verify.py incremental --work-dir PARENT
python3 test/test_verify.py
python3 test/verify.py --explain          # print the divergence families
```

`differential` runs the deterministic case table, every live `.pt` file in the
Skript checkout, and 2000 generated cases from seed 20261002 (`--cases`,
`--seed`, `--no-skript-files`). It compares the two sides token by token on
byte kind and byte range, and compares each `datum_comment` against the span
the reader's own token stream implies for the discarded form.

When the lexer oracle fails, the failure is classified and the comparison
carries on over the prefix the reader did determine: its tokens, the datum
spans they imply and the leaves the tree put there are still compared
strictly. Everything from the end of its last successful token on is the
unobserved tail — the tree lexed bytes the reader never reached — and a datum
value the failure cut in half is undetermined too, with the boundary moved
back to the `#;` marker that owns it. A datum span reaching past that point is
reported as `datum_span_not_determined`, carrying the unverified claim and the
offset it was determined up to: unverified, never waived, never fatal.

A difference is tolerated only when it lands in one of four classified
families with concrete evidence — `number_terminator_leniency`,
`misplaced_shebang`, `curly_infix_ahead_of_reader`,
`unterminated_block_comment_editor_recovery` — or in one of two informational
families, which carry evidence too and are never fatal:
`grammar_error_outside_braces` and `datum_span_not_determined`. Everything
else is unclassified and fatal:
`unclassified_reader_error`, `unclassified_token_mismatch`,
`datum_span_boundary_violation`, `opaque_span_mismatch`, `unclassified_leaf_type`,
`unclassified_incremental_mismatch`, `harness_error`. Exit status is 0 when
every difference was classified, 1 when any was not, 2 on a usage or
environment problem.

`incremental` applies edits through `ts_tree_edit` and reparses, comparing the
result against a fresh parse of the edited text: node type, field name, byte
range, row/column points, and the named, missing, error, extra and
`has_error` flags, plus child counts and opaque spans. Any disagreement is
`unclassified_incremental_mismatch`.

`test/test_verify.py` is the harness's own suite, and it is not one thing:
deterministic unit tests exercise the model's own pieces — span derivation, the
classifier, tree comparison, work-directory ownership — while integration tests
build the real lexer oracle and the real parser probe and run them over real
Skript source. The integration classes skip when Zig, a C compiler or a
tree-sitter runtime is missing instead of pretending to have verified
something; the unit classes need none of them.

### Work directory ownership

`--work-dir` names a **parent**, never a scratch to write into. Without it the
harness uses `$TREE_SITTER_SKRIPT_VERIFY_WORKDIR`, then the XDG cache, then
`/home/tmp2/omp/tree-sitter-skript-work`. `test/queries.py` follows the same
rule with `--workdir` and `$TS_QUERIES_TEST_WORKDIR`.

Every run creates exactly one fresh child of that parent, builds everything
inside it, and removes only that child — on success and on failure. Files
already in the parent are never read, overwritten or deleted; the parent is
resolved through symlinks before it is created and re-checked afterwards.
Anything resolving inside `/tmp` or `/var/tmp` is refused. `TMPDIR`, `TMP`,
`TEMP`, `XDG_CACHE_HOME` and the Zig cache directories are redirected into the
run's own child, so no cache is shared with the caller. `--keep` keeps the
child and prints its exact path.

## Development

Use [devenv](https://devenv.sh) for the `tree-sitter` CLI.

```sh
direnv allow
```

Regenerate the parser after you edit `grammar.js`. The generated parser is
ABI 15.

```sh
tree-sitter generate --abi=15
```

Run the corpus tests and the tree-sitter-native highlight expectations in
`test/highlight/skript.pt`.

```sh
tree-sitter test
```

Check the queries behaviourally: `tree-sitter parse` for clean fixtures and
`datum_comment` byte spans, `tree-sitter query` for exact outline and bracket
capture ranges, and `tree-sitter highlight --html --css-classes` for the
classes the highlighter actually resolves each byte to.

```sh
python3 test/queries.py --workdir PARENT
```

## Bindings

Each directory under `bindings/` holds one binding. The Makefile and CMake
build the C library; Cargo, Go, npm, PyPI, SwiftPM and Zig build the rest. Each
of them compiles `src/parser.c` together with `src/scanner.c`, so the scanner
is required on every surface, not just in this repository.

```sh
make                                  # libtree-sitter-skript.a, .so and .pc
make test                             # tree-sitter test

cargo test                            # bindings/rust/lib.rs
go test ./bindings/go                 # bindings/go/binding_test.go

python3 -m pip install '.[core]'   # tree-sitter~=0.25, the [core] extra
python3 -m unittest discover -s bindings/python/tests

node --test bindings/node/*_test.js   # or: npm test

zig build && zig build test           # build.zig.zon declares Zig 0.16.0
swift test
```

Each suite parses through its binding and linked core. The table separates
declared compatibility from the runtime versions exercised here.

### Runtime compatibility

The generated parser is ABI 15, so tree-sitter core 0.25 or newer is the floor
for every binding below; 0.25 is the first core line that accepts it. A binding
pins a specific core, usually a newer one, so the columns separate what is
declared from what was actually exercised.

| Binding | Declared | Observed in this checkout |
| --- | --- | --- |
| C | core >= 0.25; installed header `tree_sitter/tree-sitter-skript.h` declares `const TSLanguage *tree_sitter_skript(void)` | upstream core 0.25.0, including consumers of staged Make and CMake static/shared installs |
| Rust | `tree-sitter-language` 0.1 supplies `LANGUAGE: LanguageFn`; consumers need core >= 0.25.0; dev-dependency 0.26.8 | suite on core 0.26.13, separate consumer on exactly 0.25.0 |
| Go | `github.com/tree-sitter/go-tree-sitter` v0.25.0, module `go 1.23` | go-tree-sitter 0.25.0 |
| Node | optional peer `tree-sitter` `^0.25.0`, matched by the dev-dependency | node-tree-sitter 0.25.0 and 0.25.1 on Node 24 |
| Python | `requires-python >= 3.10`; optional `[core]` extra on `tree-sitter~=0.25` | py-tree-sitter 0.25.0 |
| Swift | `swift-tools-version:5.9`; SwiftTreeSitter from 0.10.0 to the next minor, the first release vendoring the core 0.25 family | wrapper 0.10.0 on core 0.25.10, built by Swift 6.2.4 |
| Zig | `minimum_zig_version = 0.16.0`; `zig-tree-sitter` pinned by commit hash — wrapper 0.26.0, which in turn pins tree-sitter core 0.27.0 | Zig 0.16 |

`package.json` declares no `engines` field. Node 24 is what the Node suite ran
on here, which is a fact about that run, not a minimum-Node claim.

### What each suite checks

The binding suites check consumer-visible trees. The differential harness
compares lexer tokens and derives discarded-form spans. It does not run `Reader.zig`.

- Rust, 13 cases: `defn` and dotted-list fields, comment spans and editor
  recovery, multibyte byte ranges, comments never taking a value slot, escaped
  quotes inside a discarded string, and NUL bytes inside a nested block
  comment, a discarded string, a discarded identifier (leading one included)
  and a skipped line comment.
- Go, 7 cases: the same field, span, recovery, multibyte and value-slot claims,
  with the value-slot one table-driven over a `def` value, a map value and a
  quoted form.
- Node, Python, Swift and Zig, 5 cases each: nested and unterminated block
  comment spans, the legacy `defn` arglist, the `defn` name and params fields,
  and the dotted-list `tail:` field.

### What a distribution has to carry

`src/parser.c`, `src/scanner.c`, the `src/tree_sitter/*.h` headers the parser
includes, and `queries/*.scm`. Each manifest names them: Cargo's `include`,
npm's `files`, setuptools' extension sources with the `queries` package data
and `EggInfo.find_sources`, `build.zig.zon`'s `paths` beside the sources
`build.zig` compiles, `Package.swift`'s explicit `sources` and copied `queries`
resource, and the Go module, which filters nothing.

Local Cargo, npm and Python source archives contained the scanner, parser,
all three parser headers and all four queries. The Python wheel contained the
ABI3 extension and all four queries. No package was published.

### Installing the C library

`make install PREFIX=… DESTDIR=…` and `cmake --install` lay down:

| Artefact | Destination |
| --- | --- |
| `libtree-sitter-skript.a` and the shared library, whose soname carries the ABI major, `15` | `$LIBDIR` |
| `tree_sitter/tree-sitter-skript.h` | `$INCLUDEDIR` |
| `tree-sitter-skript.pc`, generated from `bindings/c/tree-sitter-skript.pc.in` | `$LIBDIR/pkgconfig`; `make` uses `libdata/pkgconfig` on the BSDs |
| `queries/*.scm` | `$DATADIR/tree-sitter/queries/skript` |

CMake resolves those paths from `GNUInstallDirs` and builds the linkage
`BUILD_SHARED_LIBS` selects — shared by default, `SOVERSION` `15.0` — so it
installs the static archive only when that option is off. `make` builds both.

## License

MIT. See [LICENSE](LICENSE).