/* parser_probe.c - dump tree-sitter parse results as JSON lines for verify.py.
 *
 * Compiled by test/verify.py against the CURRENTLY GENERATED src/parser.c
 * (plus src/scanner.c when the grammar ships an external scanner) and the
 * installed tree-sitter runtime. Nothing here re-implements the grammar; the
 * probe only walks the syntax tree the real parser produces.
 *
 * Two modes:
 *
 *   parser_probe tree <file> [<file> ...]
 *       Parse each file from scratch. One `case` index per file.
 *
 *   parser_probe incremental <n> <file0> ... <fileN> <start0> <oldend0>
 * <newend0> ... Parse file0, then for each edit k: ts_tree_edit() the live
 * tree, reparse file(k+1) with it (the reused path), and independently parse
 *       file(k+1) from scratch. Both trees are dumped at every stage so the
 *       caller can compare them. TSPoint values are derived here from the two
 *       buffers, so the caller cannot get a point wrong and blame the parser.
 *
 * Node records (one per node, pre-order):
 *   {"case":k,"parse":"incremental"|"fresh","i":n,"depth":d,"type":T,
 *    "field":F|null,"s":..,"e":..,"sr":..,"sc":..,"er":..,"ec":..,
 *    "named":0|1,"missing":0|1,"error":0|1,"extra":0|1,"has_error":0|1,
 *    "children":n,"opaque":0|1}
 *
 * `extra` (ts_node_is_extra) and `has_error` (ts_node_has_error) are dumped
 * alongside `error` (ts_node_is_error) because a reused tree can disagree with
 * a fresh one on all three: an extra node is one the parser attached outside
 * any production, and `has_error` is set on every ANCESTOR of an ERROR, not
 * only on the ERROR itself. A dump that omitted either would make the
 * comparison silently skip a real divergence.
 *
 * An additional top-level record is emitted for every node whose type is in
 * OPAQUE_NODE_TYPES, so a caller can see the claimed span without walking the
 * subtree:
 *   {"case":k,"opaque":1,"type":T,"s":..,"e":..,"depth":d}
 */

#include <tree_sitter/api.h>

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* Defined by the generated src/parser.c. */
const TSLanguage *tree_sitter_skript(void);

/* Node types whose ENTIRE span is an opaque token: the parser claims the range
 * without the caller being able to enumerate the tokens inside it. Today that
 * is `datum_comment`, the reader's `#;` form. The verifier must check such a
 * span against the lexer output independently; it must never trust it just
 * because the tree says so. */
static const char *const OPAQUE_NODE_TYPES[] = {"datum_comment"};
static const size_t OPAQUE_NODE_TYPE_COUNT =
    sizeof(OPAQUE_NODE_TYPES) / sizeof(OPAQUE_NODE_TYPES[0]);

typedef struct {
  char *data;
  size_t len;
} Buf;

typedef struct {
  TSNode node;
  const char *field;
  uint32_t depth;
  uint32_t next_child;
  bool emitted;
} Frame;

static void die(const char *msg) {
  fprintf(stderr, "parser_probe: %s\n", msg);
  exit(4);
}

static Buf read_file(const char *path) {
  FILE *f = fopen(path, "rb");
  if (!f) {
    fprintf(stderr, "parser_probe: cannot open %s\n", path);
    exit(4);
  }
  if (fseek(f, 0, SEEK_END) != 0)
    die("fseek failed");
  long size = ftell(f);
  if (size < 0)
    die("ftell failed");
  rewind(f);
  /* +1 so text files can be handed to APIs that want a NUL, and so a zero-byte
   * file still gets a non-NULL pointer. */
  char *data = (char *)malloc((size_t)size + 1);
  if (!data)
    die("out of memory");
  size_t got = fread(data, 1, (size_t)size, f);
  if (got != (size_t)size)
    die("short read");
  data[size] = '\0';
  fclose(f);
  Buf b = {data, (size_t)size};
  return b;
}

static bool is_opaque(const char *type) {
  for (size_t i = 0; i < OPAQUE_NODE_TYPE_COUNT; i++) {
    if (strcmp(type, OPAQUE_NODE_TYPES[i]) == 0)
      return true;
  }
  return false;
}

static void json_string(const char *s) {
  putchar('"');
  for (const unsigned char *p = (const unsigned char *)s; *p != '\0'; p++) {
    switch (*p) {
    case '"':
      fputs("\\\"", stdout);
      break;
    case '\\':
      fputs("\\\\", stdout);
      break;
    case '\n':
      fputs("\\n", stdout);
      break;
    case '\r':
      fputs("\\r", stdout);
      break;
    case '\t':
      fputs("\\t", stdout);
      break;
    default:
      if (*p < 0x20) {
        printf("\\u%04x", (unsigned)*p);
      } else {
        putchar((int)*p);
      }
    }
  }
  putchar('"');
}

/* Tree-sitter columns are byte offsets inside the row, not code points and not
 * character counts. */
static TSPoint point_at(const char *data, size_t len, size_t offset) {
  TSPoint p = {0, 0};
  size_t line_start = 0;
  if (offset > len)
    offset = len;
  for (size_t i = 0; i < offset; i++) {
    if (data[i] == '\n') {
      p.row++;
      line_start = i + 1;
    }
  }
  p.column = (uint32_t)(offset - line_start);
  return p;
}

static void emit_node(uint32_t case_index, const char *parse, uint32_t index,
                      const Frame *f) {
  TSNode n = f->node;
  const char *type = ts_node_type(n);
  TSPoint sp = ts_node_start_point(n);
  TSPoint ep = ts_node_end_point(n);
  bool opaque = is_opaque(type);

  printf("{\"case\":%u,\"parse\":\"%s\",\"i\":%u,\"depth\":%u,\"type\":",
         case_index, parse, index, f->depth);
  json_string(type);
  fputs(",\"field\":", stdout);
  if (f->field == NULL) {
    fputs("null", stdout);
  } else {
    json_string(f->field);
  }
  printf(",\"s\":%u,\"e\":%u,\"sr\":%u,\"sc\":%u,\"er\":%u,\"ec\":%u,"
         "\"named\":%d,\"missing\":%d,\"error\":%d,\"extra\":%d,"
         "\"has_error\":%d,\"children\":%u,\"opaque\":%d}\n",
         ts_node_start_byte(n), ts_node_end_byte(n), sp.row, sp.column, ep.row,
         ep.column, ts_node_is_named(n) ? 1 : 0, ts_node_is_missing(n) ? 1 : 0,
         ts_node_is_error(n) ? 1 : 0, ts_node_is_extra(n) ? 1 : 0,
         ts_node_has_error(n) ? 1 : 0, ts_node_child_count(n), opaque ? 1 : 0);

  if (opaque) {
    printf("{\"case\":%u,\"parse\":\"%s\",\"opaque\":1,\"type\":", case_index,
           parse);
    json_string(type);
    printf(",\"s\":%u,\"e\":%u,\"depth\":%u}\n", ts_node_start_byte(n),
           ts_node_end_byte(n), f->depth);
  }
}

static void dump_tree(uint32_t case_index, const char *parse, TSNode root) {
  size_t cap = 256, len = 0;
  Frame *stack = (Frame *)malloc(cap * sizeof(Frame));
  if (!stack)
    die("out of memory");
  stack[len++] = (Frame){root, NULL, 0, 0, false};
  uint32_t index = 0;

  while (len > 0) {
    Frame *top = &stack[len - 1];
    if (!top->emitted) {
      top->emitted = true;
      emit_node(case_index, parse, index++, top);
    }
    if (top->next_child < ts_node_child_count(top->node)) {
      uint32_t c = top->next_child++;
      const char *field = ts_node_field_name_for_child(top->node, c);
      TSNode child = ts_node_child(top->node, c);
      uint32_t depth = top->depth + 1;
      if (len == cap) {
        cap *= 2;
        Frame *grown = (Frame *)realloc(stack, cap * sizeof(Frame));
        if (!grown)
          die("out of memory");
        stack = grown;
      }
      stack[len++] = (Frame){child, field, depth, 0, false};
    } else {
      len--;
    }
  }
  free(stack);
}

static TSParser *make_parser(void) {
  TSParser *parser = ts_parser_new();
  if (!parser)
    die("ts_parser_new failed");
  if (!ts_parser_set_language(parser, tree_sitter_skript())) {
    fprintf(stderr,
            "parser_probe: ts_parser_set_language failed; parser ABI %u, "
            "runtime supports %u..%u\n",
            ts_language_abi_version(tree_sitter_skript()),
            TREE_SITTER_MIN_COMPATIBLE_LANGUAGE_VERSION,
            TREE_SITTER_LANGUAGE_VERSION);
    exit(5);
  }
  return parser;
}

static TSTree *parse(TSParser *parser, TSTree *old_tree, const Buf *b) {
  return ts_parser_parse_string(parser, old_tree, b->data, (uint32_t)b->len);
}

static int mode_tree(int argc, char **argv) {
  if (argc < 3) {
    fprintf(stderr, "parser_probe: tree needs at least one file\n");
    return 2;
  }
  TSParser *parser = make_parser();
  for (int i = 2; i < argc; i++) {
    Buf b = read_file(argv[i]);
    TSTree *tree = parse(parser, NULL, &b);
    if (!tree) {
      fprintf(stderr, "parser_probe: parse returned NULL for %s\n", argv[i]);
      return 6;
    }
    dump_tree((uint32_t)(i - 2), "fresh", ts_tree_root_node(tree));
    ts_tree_delete(tree);
    free(b.data);
  }
  ts_parser_delete(parser);
  return 0;
}

static uint32_t parse_u32(const char *s) {
  char *end = NULL;
  unsigned long v = strtoul(s, &end, 10);
  if (end == s || *end != '\0')
    die("expected an unsigned integer argument");
  return (uint32_t)v;
}

static int mode_incremental(int argc, char **argv) {
  if (argc < 3)
    die("incremental needs an edit count");
  uint32_t n = parse_u32(argv[2]);
  /* argv[0] + argv[1] mode + argv[2] count + (n+1) files + 3n offsets */
  if ((uint32_t)argc != 4u + 4u * n) {
    fprintf(stderr,
            "parser_probe: incremental argument count mismatch: expected %u, "
            "got %d\n",
            4u + 4u * n, argc);
    return 2;
  }

  /* argv: [prog][mode][n][file0..fileN][start0 oldend0 newend0]... */
  char **paths = argv + 3;
  uint32_t *offsets = (uint32_t *)malloc(sizeof(uint32_t) * 3u * n);
  Buf *texts = (Buf *)malloc(sizeof(Buf) * (n + 1u));
  if (!offsets || !texts)
    die("out of memory");
  for (uint32_t i = 0; i <= n; i++)
    texts[i] = read_file(paths[i]);
  for (uint32_t k = 0; k < 3u * n; k++) {
    offsets[k] = parse_u32(paths[(n + 1u) + k]);
  }

  TSParser *parser = make_parser();
  TSTree *tree = parse(parser, NULL, &texts[0]);
  if (!tree)
    die("initial parse returned NULL");

  for (uint32_t k = 0; k < n; k++) {
    TSInputEdit edit;
    edit.start_byte = offsets[3u * k];
    edit.old_end_byte = offsets[3u * k + 1u];
    edit.new_end_byte = offsets[3u * k + 2u];
    edit.start_point = point_at(texts[k].data, texts[k].len, edit.start_byte);
    edit.old_end_point =
        point_at(texts[k].data, texts[k].len, edit.old_end_byte);
    edit.new_end_point =
        point_at(texts[k + 1u].data, texts[k + 1u].len, edit.new_end_byte);
    if (edit.start_byte > edit.old_end_byte ||
        edit.old_end_byte > texts[k].len) {
      fprintf(stderr, "parser_probe: edit %u out of range for %s\n", k,
              paths[k]);
      return 7;
    }

    ts_tree_edit(tree, &edit);
    TSTree *incremental = parse(parser, tree, &texts[k + 1u]);
    if (!incremental) {
      fprintf(stderr, "parser_probe: incremental parse returned NULL at %u\n",
              k);
      return 6;
    }
    TSTree *fresh = parse(parser, NULL, &texts[k + 1u]);
    if (!fresh) {
      fprintf(stderr, "parser_probe: fresh parse returned NULL at %u\n", k);
      return 6;
    }
    dump_tree(k, "incremental", ts_tree_root_node(incremental));
    dump_tree(k, "fresh", ts_tree_root_node(fresh));
    ts_tree_delete(fresh);
    ts_tree_delete(tree);
    tree = incremental;
  }
  ts_tree_delete(tree);
  ts_parser_delete(parser);

  for (uint32_t i = 0; i <= n; i++)
    free(texts[i].data);
  free(texts);
  free(offsets);
  return 0;
}

int main(int argc, char **argv) {
  if (argc < 2) {
    fprintf(stderr, "usage: parser_probe tree <file>...\n"
                    "       parser_probe incremental <n> <file0> .. <fileN> "
                    "<start0> <oldend0> <newend0> ...\n");
    return 2;
  }
  if (strcmp(argv[1], "tree") == 0)
    return mode_tree(argc, argv);
  if (strcmp(argv[1], "incremental") == 0)
    return mode_incremental(argc, argv);
  fprintf(stderr, "parser_probe: unknown mode %s\n", argv[1]);
  return 2;
}
