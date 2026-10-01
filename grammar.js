/**
 * @file Skript Tree-Sitter Grammar
 * @author CoffeeImpliesCode <coffeeimpliescode@proton.me>
 * @license MIT
 *
 * The rules below mirror skript's READER (src/Token.zig + src/Reader.zig), not
 * its compiler. Two deliberate departures are documented in README.md:
 * `def`/`defn`/`fn` get named nodes the reader does not have, and `{}` is
 * parsed as curly-infix ahead of the reader (per skript's own
 * docs/notation-design.md), which still reports `error.Unimplemented` for it.
 */

/// <reference types="tree-sitter-cli/dsl" />
// @ts-check

// The reader's list production: `'(' form* [ '.' form ] ')'`
// (skript/src/Reader.zig:413-476). Two alternatives so a LEADING dot cannot
// parse: the reader rejects `(. a)` (Reader.zig:453) because a dotted tail
// needs a preceding element.
const listBody = $ => choice(
  seq(repeat1($.expression), optional(seq('.', field('tail', $.expression)))),
  repeat($.expression),
);

export default grammar({
  name: "skript",

  word: $ => $.identifier,

  supertypes: $ => [ $.expression, $.atom ],

  extras: $ => [/\s/, $.line_comment, $.block_comment],

  rules: {
    source_file: $ => seq(
      optional($.shebang),
      repeat($.expression),
    ),

    // `;` runs to end of line (Token.zig:189).
    line_comment: _ => token(seq(';', /[^\n]*/)),

    // `#| ... |#` block comment (Token.zig:157-160). Deliberately NOT nested:
    // the reader counts depth (Token.zig:216-231) and tree-sitter cannot
    // express that without an external scanner - and an external token listed
    // in `extras` is only consulted at byte offset 0, so it would parse the
    // same text as a comment in one position and as three identifiers in
    // every other. The flat token is wrong only for the nested spelling.
    //
    // prec(1) beats the identifier rule on the `#|` opener, so the no-space
    // spelling `#|a|#` is a comment and not one long identifier.
    block_comment: _ => token(prec(1, seq(
      '#|',
      repeat(choice(/[^#|]/, /#[^|]/, /\|[^#]/)),
      '|#',
    ))),

    // The reader only recognises `#!` at byte offset 0 (Token.zig:190-195) and
    // treats it as an identifier anywhere else. The grammar cannot anchor a
    // token to offset 0, so this matches `#!` at the start of any top-level
    // line: `\n#!x\n42` reads as (shebang) + 42 where the reader gives two
    // identifiers. Mid-line (`a #!b c`) stays identifiers. See README.md.
    shebang: _ => token(prec(1, /#![^\n]*/)),

    // (def NAME VALUE). The reader treats `def` as an ordinary symbol, so the
    // value is optional and extra forms are allowed - anything the reader
    // accepts must parse here.
    def: $ => prec(1, choice(
      seq('(', "def", field('name', $.identifier), field('value', $.expression), repeat($.expression), ')'),
      seq('(', "def", field('name', $.identifier), repeat($.expression), ')'),
    )),

    // Modern shape: (defn NAME (ARGS) BODY...)
    // Degraded shape (no arglist, avoids ERROR-recovery tail-eating):
    //   (defn NAME BODY...)
    // Legacy shape (still used by lib/raylib.pt): (defn (NAME ARGS) BODY...)
    defn: $ => prec(1, choice(
      seq('(', "defn", field('name', $.identifier), field('params', $.list), repeat($.expression), ')'),
      seq('(', "defn", field('name', $.identifier), repeat($.expression), ')'),
      seq('(', "defn", field('params', $.list), repeat($.expression), ')'),
    )),

    // (fn (ARGS) BODY...), plus (fn BODY...) for the argless spellings the
    // corpus uses, e.g. `(fn rest rest)` in test/api/semantics.pt. `fn` is an
    // ordinary symbol to the reader, so no head shape is mandatory.
    fn: $ => prec(1, choice(
      seq('(', "fn", field('params', $.list), repeat($.expression), ')'),
      seq('(', "fn", repeat($.expression), ')'),
    )),

    // Generic list form - head-symbol meaning lives in queries/highlights.scm.
    call: $ => seq('(', listBody($), ')'),

    // Arglist node, reachable only from defn/fn - scopes @variable.parameter.
    // prec(1) beats call on the reduce conflict so defn/fn arglists parse as
    // `list`, not as a generic `call` body.
    list: $ => prec(1, seq('(', listBody($), ')')),

    // [ e ... ] - tensor literal; the reader desugars it to (stack ...).
    array_lit: $ => seq('[', repeat($.expression), ']'),

    // { e op e ... } - SRFI-105 exclusive curly-infix. Ahead of the reader:
    // skript's own docs/notation-design.md specifies this rule, and the reader
    // still answers error.Unimplemented for a bare `{` (Reader.zig:408).
    infix_expr: $ => seq('{', repeat($.expression), '}'),

    // :{ :k v ... } - map literal with keyword keys; the reader desugars it to
    // (hash-map :k v ...). `:{` is a distinct token because it outranks
    // kwd_lit, whose body excludes `{`.
    map_marker: _ => token(/:\{/),
    map_lit: $ => seq($.map_marker, repeat(seq(field('key', $.kwd_lit), field('value', $.expression))), '}'),

    // #; FORM - datum comment: the reader reads the form and throws it away
    // (Reader.zig:355-363). Modelled as a real form so the discarded node is
    // visible instead of being swallowed by the line-comment rule.
    datum_comment: $ => seq('#;', $.expression),

    expression: $ => choice(
      $.def,
      $.defn,
      $.fn,
      $.call,
      $.array_lit,
      $.infix_expr,
      $.map_lit,
      $.datum_comment,
      $.atom,
    ),

    atom: $ => choice(
      $.identifier,
      $.num_lit,
      $.string_lit,
      $.kwd_lit,
      $.bool_lit,
      $.nil_lit,
      $.quote_lit,
    ),

    // ' FORM - reader macro for (quote FORM) (Reader.zig:364-405).
    quote_lit: $ => seq("'", $.expression),

    // Mirrors Token.zig lexIdentifier (252-264). The FIRST character excludes
    // the delimiters the lexer's dispatch switch claims (Token.zig:115-179) -
    // whitespace, ()[]{} ".', digits and `.` - so `,` and `@` and `#` may
    // start an identifier. Continues until whitespace or one of ()[]{};",
    // which is why `'` and `,` and `:` are legal INSIDE one.
    // The FIRST character excludes `'` (it opens quote_lit) but the BODY does
    // not: Token.zig:257 terminates an identifier on whitespace and
    // ()[]{}" only, never on `'` itself, so `x'y` is one symbol to skript.
    // Keeping `'` out of the body class makes tree-sitter split it into
    // `x` + quote + `y`.
    identifier: _ => /[^ \t\r\n()\[\]{};"'.:0-9][^ \t\r\n()\[\]{};"]*/,

    // One token mirroring Token.zig's number dispatch.
    num_lit: _ => token(prec(10, choice(
      // Signed special float, which MAY carry a complex imaginary part
      // (Token.zig:275-292). No exponent there: the reader eats digits, an
      // optional fraction, then `i`, and backtracks otherwise.
      seq(/[+-]/, /(inf|nan)\.0/, optional(seq(/[+-]/, /[0-9]+(\.[0-9]+)?/, 'i'))),
      // Unsigned inf.0/nan.0: matchSpecialFloat returns immediately
      // (Token.zig:403-412), so `nan.0-1.0i` is TWO tokens, not one.
      /(inf|nan)\.0/,
      // Radix integers, digits constrained per base (Token.zig:433-452).
      // `#b2` is a reader error, not a two-character identifier.
      seq('#', choice(
        seq('b', /[01]+/),
        seq('o', /[0-7]+/),
        seq('d', /[0-9]+/),
        seq('x', /[0-9a-fA-F]+/),
      )),
      // Ordinary number: optional sign, int/float/exponent, optional `i`
      // suffix or real+imag pair (Token.zig:296-362).
      seq(
        optional(/[+-]/),
        /[0-9]+(\.[0-9]+)?([eE][+-]?[0-9]+)?/,
        optional(choice(
          seq(/[+-]/, /[0-9]+(\.[0-9]+)?([eE][+-]?[0-9]+)?/, 'i'),
          'i',
        )),
      ),
    ))),

    // "..." with opaque escapes - `\` escapes the next byte (Token.zig:374-391).
    string_lit: _ => token(seq('"', repeat(choice(/[^"\\]/, seq('\\', /./))), '"')),

    // :NAME - the body stops at exactly the reader's terminator set
    // (Token.zig:243), so `:`, `@`, `,`, `'` and `.` are all legal inside a
    // keyword. Zero-width `:(` cannot match, which is what the reader's
    // "zero-width keyword" rejection (Reader.zig:606-607) amounts to.
    kwd_lit: _ => token(seq(':', /[^ \t\r\n()\[\]{};"]+/)),

    // Plain strings, not `token(...)`: `word: $.identifier` makes tree-sitter
    // extract them as keywords, so `#true` stays ONE identifier while bare
    // `#t` matches. The reader is byte-exact on `#t`/`#f`/`nil`
    // (Reader.zig:610-615) - `#T` is an ordinary symbol.
    bool_lit: _ => choice('#t', '#f'),
    nil_lit: _ => 'nil',
  }
});