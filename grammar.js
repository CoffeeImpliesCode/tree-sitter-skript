/**
 * @file Skript Tree-Sitter Grammar
 * @author CoffeeImpliesCode <coffeeimpliescode@proton.me>
 * @license MIT
 */

/// <reference types="tree-sitter-cli/dsl" />
// @ts-check

export default grammar({
  name: "skript",

  word: $ => $.identifier,

  supertypes: $ => [ $.expression, $.atom ],

  extras: $ => [/\s/, $.line_comment ],

  rules: {
    source_file: $ => seq(
      optional($.shebang),
      repeat($.expression),
    ),

    line_comment: $ => token(seq(';', /[^\n]*/)),

    shebang: _ => token(prec(1, /#![^\n]*/)),

    // (def NAME VALUE)
    def: $ => prec(1, seq('(', "def", field('name', $.identifier), field('value', $.expression), ')')),
    // Modern shape: (defn NAME (ARGS...) BODY...)
    // Degraded shape (missing arglist, avoids ERROR-recovery tail-eating):
    //   (defn NAME BODY...)
    // Legacy shape (still used by lib/raylib.pt): (defn (NAME ARGS...) BODY...)
    defn: $ => prec(1, choice(
      prec(1, seq('(', "defn", field('name', $.identifier), field('params', $.list), repeat($.expression), ')')),
      prec(0, seq('(', "defn", field('name', $.identifier), repeat($.expression), ')')),
      seq('(', "defn", field('params', $.list), repeat($.expression), ')'),
    )),
    // (fn (ARGS...) BODY...)
    fn: $ => prec(1, seq('(', "fn", field('params', $.list), repeat($.expression), ')')),
    // Generic list form — head-symbol meaning lives in queries/highlights.scm.
    call: $ => seq('(', repeat($.expression), ')'),
    // Arglist node, reachable only from defn/fn — scopes @variable.parameter.
    // prec(1) beats call on the reduce conflict so defn/fn arglists parse as
    // `list`, not as a generic `call` body.
    list: $ => prec(1, seq('(', repeat($.expression), ')')),

    expression: $ => choice(
      $.def,
      $.defn,
      $.fn,
      $.call,
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

    // ' FORM — reader macro for (quote FORM).
    quote_lit: $ => seq("'", $.expression),

    // Mirrors src/Token.zig's identifier surface: starts with any non-delimiter
    // (never whitespace, ( ) [ ] { } ; " ' , : or a digit), continues until
    // whitespace or ( ) [ ] { } ; ". A leading @ marks intrinsics
    // (@rgba, @i64.add); ':' in the body gives qualified names
    // (dynamic-library:open, ffi:make-func); non-ASCII is allowed (the
    // reader's lexer accepts any non-delimiter byte).
    identifier: _ => /[^ \t\r\n()\[\]{};"',:0-9][^ \t\r\n()\[\]{};"']*/,

    // One token mirroring Reader.zig's parseNumber surface: optional sign,
    // #b/#o/#d/#x radix, ints/floats/exponents, inf.0/nan.0, complex i-suffix.
    // prec(10) beats the identifier rule on [+-] and i/n leading forms.
    num_lit: _ => token(prec(10, seq(
      optional(/[+-]/),
      choice(
        seq(/(inf|nan)\.0/, optional(seq(/[+-]/, /[0-9]+(\.[0-9]+)?([eE][+-]?[0-9]+)?/, 'i'))),
        seq('#', /[bodx]/, /[0-9a-fA-F]+/),
        seq(
          /[0-9]+(\.[0-9]+)?([eE][+-]?[0-9]+)?/,
          optional(choice(
            seq(/[+-]/, /[0-9]+(\.[0-9]+)?([eE][+-]?[0-9]+)?/, 'i'),
            'i',
          )),
        ),
      ),
    ))),

    // "..." with opaque escapes \n \t \r \\ \" \x{..} \u{..}.
    string_lit: _ => token(seq('"', repeat(choice(/[^"\\]/, seq('\\', /./))), '"')),

    // :NAME — skript keywords are unqualified.
    kwd_lit: _ => token(seq(':', /[^ \t\r\n()\[\]{};\"',:@]+/)),

    // #t / #f (+ the Scheme-legal #T / #F). prec(1) beats the identifier
    // rule on the bare two-char forms.
    bool_lit: _ => token(prec(1, seq('#', /[tTfF]/))),

    nil_lit: _ => token(prec(1, 'nil')),
  }
});
