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
    // TODO: add the actual grammar rules
    source_file: $ => seq(
      optional($.shebang),
      repeat($.expression),
    ),
    line_comment: $ => seq(';', /[^\n]*\n/),
    // statement: $ => choice($.def, $.defn, $.fn, $.expression),
    def: $ => seq('(', "def", $.identifier, $.expression, ')'),
    defn: $ => seq('(', "defn", $.list, $.expression, ')'),
    fn: $ => seq('(', "fn", $.list, $.expression, ')'),
    call: $ => seq('(', repeat1($.expression), ')'),
    list: $ => seq('(', repeat1($.expression), ')'),
    expression: $ => choice(
      $.def,
      $.defn,
      $.fn,
      $.call,
      $.atom,
    ),
    atom: $ => choice($.identifier, $.literal),
    literal: $ => choice(
      $.symbol_literal,
      $.integer_literal,
      $.decimal_literal,
    ),
    symbol_literal: _ => /'\W+/,
    integer_literal: _ => /\d+/,
    decimal_literal: _ => /\d+\.\d+/,
    identifier: _ => /[a-zA-Z\+\-\*\/><=?][a-zA-Z0-9\+\-\*\/><=?]*/,
    shebang: _ => /#![\s]*[^\[].+/,
  }
});
