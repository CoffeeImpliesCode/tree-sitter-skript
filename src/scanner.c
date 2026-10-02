// External scanner for the two comment forms skript's own lexer owns.
//
// Both are `extras`, so tree-sitter asks for them at every token position -
// exactly where `lex` re-dispatches after skipping comments
// (Token.zig:108-179), and never inside a string or an identifier body, which
// are single tokens.
//
// Stateless: both forms are decided by the input alone, so create() is NULL and
// serialize() writes nothing. Collection closers use inline storage and spill
// to a heap buffer only beyond INLINE_CLOSERS.

#include "tree_sitter/parser.h"

#include <stdint.h>
#include <stdlib.h>
#include <string.h>

// Order MUST match the `externals` array in grammar.js.
enum TokenType {
  BLOCK_COMMENT,
  DATUM_COMMENT,
};

// Collection depth kept on the stack; deeper input grows a heap buffer.
#define INLINE_CLOSERS 16

// The reader's whitespace set (Token.zig:188), spelled out rather than
// iswspace() so the scanner never depends on the host locale.
static inline bool is_space(int32_t c) {
  return c == ' ' || c == '\t' || c == '\r' || c == '\n';
}

// The characters that end an identifier or keyword body (Token.zig:243 and
// Token.zig:257). `'` is NOT one of them: `x'y` is one symbol to skript.
static inline bool ends_atom(int32_t c) {
  switch (c) {
  case ' ':
  case '\t':
  case '\r':
  case '\n':
  case '(':
  case ')':
  case '[':
  case ']':
  case '{':
  case '}':
  case ';':
  case '"':
    return true;
  default:
    return false;
  }
}

// Consumes one character and returns the next. Use eof() to detect end of
// input: lookahead 0 is also an ordinary NUL byte in the source.
static inline int32_t take(TSLexer *lexer) {
  lexer->advance(lexer, false);
  return lexer->lookahead;
}

// The rest of a `#| ... |#` comment, opener already consumed. Mirrors
// skipBlockComment (Token.zig:216-231) including its nesting depth, but stops
// at end of input instead of failing: an unterminated comment owns everything
// after it so the editor keeps working. A deliberate departure from the reader,
// which answers error.UnexpectedToken (Token.zig:220).
static void scan_block_comment_body(TSLexer *lexer) {
  uint32_t depth = 1;
  for (;;) {
    if (lexer->eof(lexer))
      return;
    if (lexer->lookahead == '#') {
      take(lexer);
      if (lexer->lookahead == '|') {
        take(lexer);
        depth++;
      }
    } else if (lexer->lookahead == '|') {
      take(lexer);
      if (lexer->lookahead == '#') {
        take(lexer);
        if (--depth == 0)
          return;
      }
    } else {
      take(lexer);
    }
  }
}

// Whitespace and `;` line comments including their newline (Token.zig:185-212),
// which the reader steps over in front of every form. `#|` is NOT skipped here:
// peeking past a `#` consumes it, and the caller decides whether it opened a
// block comment, a datum marker or an atom. Nothing in here is marked, so a
// span stops at its last meaningful token instead of trailing whitespace.
static void skip_blanks(TSLexer *lexer) {
  for (;;) {
    if (is_space(lexer->lookahead)) {
      take(lexer);
    } else if (lexer->lookahead == ';') {
      for (;;) {
        int32_t c = take(lexer);
        if (lexer->eof(lexer) || c == '\n')
          break;
      }
    } else {
      return;
    }
  }
}

// An identifier or keyword body, first character already consumed and `c`
// being the one after it. `.` is not a terminator, so `foo.bar` is one atom;
// `"` is, so `foo"bar"` is `foo` then a string (Token.zig:257).
static void scan_atom_body(TSLexer *lexer, int32_t c) {
  while (!lexer->eof(lexer) && !ends_atom(c)) {
    lexer->advance(lexer, false);
    c = lexer->lookahead;
  }
}

// The body of a string, opening quote already consumed, so `lookahead` is the
// first content byte. Each byte is examined BEFORE it is consumed, exactly as
// lexString does (Token.zig:374-391): a `"` closes the string and is consumed
// with it, `\` eats the byte after it and that byte may be a newline, anything
// else is content. False when the input ends first, which Token.zig:390 treats
// as an error.
static bool scan_string_body(TSLexer *lexer) {
  for (;;) {
    int32_t c = lexer->lookahead;
    if (lexer->eof(lexer))
      return false;
    take(lexer);
    if (c == '"')
      return true;
    // A `\` at the very end has nothing left to escape.
    if (c == '\\') {
      if (lexer->eof(lexer))
        return false;
      take(lexer);
    }
  }
}

// The closer of every open collection, exact at any depth. `size` is the
// depth. Both counters are `size_t`: the byte count is a `size_t` too, so
// keeping the counters in the same width is what makes the bound below
// comparable at all.
typedef struct {
  int32_t *levels;
  int32_t inline_[INLINE_CLOSERS];
  size_t size;
  size_t capacity;
  bool spilled;
} CloserStack;

static void closer_stack_init(CloserStack *stack) {
  stack->levels = stack->inline_;
  stack->size = 0;
  stack->capacity = INLINE_CLOSERS;
  stack->spilled = false;
}

// False when the buffer cannot grow: the depth count would overflow, the byte
// count would overflow, or the allocation failed. `levels` is left pointing at
// storage the caller still owns, so the caller can free it. The caller then
// abandons the token instead of emitting a partial form.
static bool closer_stack_push(CloserStack *stack, int32_t close) {
  if (stack->size == stack->capacity) {
    // One bound covers both products: `capacity` doubles, and the result is
    // scaled by the element size, so neither may pass it. The counters are
    // `size_t` for exactly this reason - a narrower counter only makes the
    // bound a comparison that can never fail, and a wider one would ask it
    // about arithmetic it never performs.
    if (stack->capacity > SIZE_MAX / (2u * sizeof(*stack->levels)))
      return false;
    size_t capacity = stack->capacity * 2u;
    size_t bytes = capacity * sizeof(*stack->levels);
    if (stack->spilled) {
      int32_t *levels = realloc(stack->levels, bytes);
      if (levels == NULL)
        return false;
      stack->levels = levels;
    } else {
      int32_t *levels = malloc(bytes);
      if (levels == NULL)
        return false;
      memcpy(levels, stack->inline_, sizeof(stack->inline_));
      stack->levels = levels;
      stack->spilled = true;
    }
    stack->capacity = capacity;
  }
  stack->levels[stack->size++] = close;
  return true;
}

// Releases the heap buffer, leaving the inline one alone.
static void closer_stack_free(CloserStack *stack) {
  if (stack->spilled)
    free(stack->levels);
}

// `#;` is consumed. The token spans the marker and exactly the ONE form the
// reader discards (Reader.zig:355-363), so `#;#;1 2 3` is one opaque `#;#;1 2`
// and leaves `3` live.
//
// Two invariants:
//   need  - forms the marker chain still owes at the OUTERMOST level. A marker
//           discharges one obligation and creates two; `'` is a prefix rather
//           than a form (Reader.zig:364-405), so it leaves the count alone and
//           its operand is the form; a collection discharges one when it
//           closes. A marker INSIDE a collection changes nothing: the forms it
//           eats belong to that collection, whose closer decides where the
//           value ends.
//   stack - the closer of every open collection. A closer this stack does not
//           own ends the value before it, so the next live form still parses.
static bool scan_datum_comment(TSLexer *lexer) {
  lexer->mark_end(lexer);

  uint32_t need = 1;
  CloserStack stack;
  closer_stack_init(&stack);

  for (;;) {
    skip_blanks(lexer);
    if (lexer->eof(lexer))
      // Retain the marker and last meaningful token when its value is
      // incomplete. The reader rejects this; the editor can still style it.
      goto emit;

    switch (lexer->lookahead) {
    case '#': {
      int32_t next = take(lexer);
      if (next == '|') {
        // A block comment is not a form: the reader's lexer eats it before
        // dispatching (Token.zig:157-160).
        take(lexer);
        scan_block_comment_body(lexer);
        continue;
      }
      if (next == ';') {
        take(lexer);
        lexer->mark_end(lexer);
        if (stack.size == 0)
          need++;
        continue;
      }
      // Any other `#` opens an atom: `#t`, `#b1010`, `#foo`.
      scan_atom_body(lexer, next);
      goto form_done;
    }

    case ':': {
      int32_t next = take(lexer);
      if (next != '{') {
        // `:name` - the body stops at the same terminator set (Token.zig:243).
        scan_atom_body(lexer, next);
        goto form_done;
      }
      take(lexer);
      lexer->mark_end(lexer);
      if (!closer_stack_push(&stack, '}'))
        goto abandon;
      continue;
    }

    case '(':
    case '[':
    case '{': {
      int32_t open = lexer->lookahead;
      take(lexer);
      lexer->mark_end(lexer);
      if (!closer_stack_push(&stack,
                             open == '(' ? ')' : (open == '[' ? ']' : '}')))
        goto abandon;
      continue;
    }

    case ')':
    case ']':
    case '}': {
      if (stack.size == 0)
        goto emit;
      if (lexer->lookahead != stack.levels[stack.size - 1])
        goto emit;
      take(lexer);
      lexer->mark_end(lexer);
      stack.size--;
      if (stack.size == 0) {
        need--;
        if (need == 0)
          goto emit;
      }
      continue;
    }

    case '\'':
      take(lexer);
      lexer->mark_end(lexer);
      continue;

    case '"':
      take(lexer);
      lexer->mark_end(lexer);
      if (!scan_string_body(lexer)) {
        lexer->mark_end(lexer);
        goto emit;
      }
      goto form_done;

    case '.':
      // A lone period is its own token (Token.zig:120-126) and is not an atom
      // terminator, so `foo.bar` must not be split here.
      take(lexer);
      goto form_done;

    default:
      scan_atom_body(lexer, take(lexer));
      goto form_done;
    }

  form_done:
    // The form the reader throws away ends HERE, so the marker and the one
    // form it owns are the whole token. Nothing after it is marked, so blanks,
    // line comments and block comments that merely trail the value stay
    // outside the span and lex again as the extras they are.
    lexer->mark_end(lexer);
    if (stack.size == 0) {
      need--;
      if (need == 0)
        goto emit;
    }
    continue;
  }

abandon:
  // A resource failure is not a comment: report no token rather than a partial
  // one, so the caller never mistakes exhaustion for a scanned form.
  closer_stack_free(&stack);
  return false;

emit:
  closer_stack_free(&stack);
  lexer->result_symbol = DATUM_COMMENT;
  return true;
}

bool tree_sitter_skript_external_scanner_scan(void *payload, TSLexer *lexer,
                                              const bool *valid_symbols) {
  (void)payload;

  while (is_space(lexer->lookahead))
    lexer->advance(lexer, true);
  if (lexer->lookahead != '#')
    return false;

  int32_t next = take(lexer);
  if (next == '|') {
    if (!valid_symbols[BLOCK_COMMENT])
      return false;
    take(lexer);
    scan_block_comment_body(lexer);
    lexer->mark_end(lexer);
    lexer->result_symbol = BLOCK_COMMENT;
    return true;
  }
  if (next == ';') {
    if (!valid_symbols[DATUM_COMMENT])
      return false;
    take(lexer);
    return scan_datum_comment(lexer);
  }
  return false;
}

void *tree_sitter_skript_external_scanner_create(void) { return NULL; }

void tree_sitter_skript_external_scanner_destroy(void *payload) {
  (void)payload;
}

unsigned tree_sitter_skript_external_scanner_serialize(void *payload,
                                                       char *buffer) {
  (void)payload;
  (void)buffer;
  return 0;
}

void tree_sitter_skript_external_scanner_deserialize(void *payload,
                                                     const char *buffer,
                                                     unsigned length) {
  (void)payload;
  (void)buffer;
  (void)length;
}