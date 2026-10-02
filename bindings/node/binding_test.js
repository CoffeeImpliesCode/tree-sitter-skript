import assert from "node:assert";
import { test } from "node:test";
import Parser from "tree-sitter";

const Skript = (await import("./index.js")).default;

// `#| a #| b |# c |#` only closes at the matching depth, so the whole run is a
// single comment node and the form behind it must still parse as code.
const NESTED_COMMENT = "#| outer #| inner |# tail |# 42";
const NESTED_COMMENT_TEXT = "#| outer #| inner |# tail |#";

// An unterminated opener owns the rest of the input instead of being rejected,
// so no live code behind it is ever highlighted as code.
const UNTERMINATED_COMMENT = "(+ 1 2) #| never closed\n(+ 3 4)";
const UNTERMINATED_COMMENT_TEXT = "#| never closed\n(+ 3 4)";

const LEGACY_DEFN = "(defn (rgba r g) (@rgba r g))";
const MODERN_DEFN = "(defn add (a b) (+ a b))";
const DOTTED_CALL = "(a b . c)";

// Parsing through this binding checks that its runtime accepts this parser ABI.
// Do not rely on the exception type or timing of a rejected language.
// Neither Parser nor Tree has a delete() - both use GC finalizers -
// so every test owns its own pair and drops them when it returns. startIndex
// and endIndex are offsets into the parsed string, which for the ASCII sources
// below are also the byte offsets.
function parse(source) {
  const parser = new Parser();
  parser.setLanguage(Skript);
  return parser.parse(source);
}

test("nested block comment spans the whole comment and keeps the following form", () => {
  const tree = parse(NESTED_COMMENT);
  const root = tree.rootNode;
  assert.strictEqual(root.hasError, false);

  const children = root.namedChildren;
  assert.deepStrictEqual(children.map((child) => child.type), ["block_comment", "num_lit"]);

  const [comment, following] = children;
  assert.strictEqual(comment.text, NESTED_COMMENT_TEXT);
  assert.deepStrictEqual([comment.startIndex, comment.endIndex], [0, 28]);
  assert.strictEqual(following.text, "42");
  assert.deepStrictEqual([following.startIndex, following.endIndex], [29, 31]);
});

test("unterminated block comment owns the rest of the input", () => {
  const tree = parse(UNTERMINATED_COMMENT);
  const root = tree.rootNode;
  assert.strictEqual(root.hasError, false);

  // Exactly two forms: the closed call, then the comment that swallowed the
  // second line. Nothing behind the opener reads as code.
  const children = root.namedChildren;
  assert.deepStrictEqual(children.map((child) => child.type), ["call", "block_comment"]);

  const [call, comment] = children;
  assert.strictEqual(call.text, "(+ 1 2)");
  assert.deepStrictEqual([call.startIndex, call.endIndex], [0, 7]);
  assert.strictEqual(comment.text, UNTERMINATED_COMMENT_TEXT);
  assert.deepStrictEqual([comment.startIndex, comment.endIndex], [8, 31]);
});

test("legacy defn params carry text and byte range", () => {
  const tree = parse(LEGACY_DEFN);
  const root = tree.rootNode;
  assert.strictEqual(root.hasError, false);

  const defn = root.namedChildren[0];
  assert.strictEqual(defn.type, "defn");
  // The legacy shape carries no name field; the bound name is the first
  // arglist element, which is what the highlights query keys on.
  assert.strictEqual(defn.childForFieldName("name"), null);

  const params = defn.childForFieldName("params");
  assert.strictEqual(params.type, "list");
  assert.strictEqual(params.text, "(rgba r g)");
  assert.deepStrictEqual([params.startIndex, params.endIndex], [6, 16]);

  const names = params.namedChildren;
  assert.deepStrictEqual(names.map((name) => name.text), ["rgba", "r", "g"]);
  assert.deepStrictEqual([names[0].startIndex, names[0].endIndex], [7, 11]);
  assert.deepStrictEqual([names[1].startIndex, names[1].endIndex], [12, 13]);
  assert.deepStrictEqual([names[2].startIndex, names[2].endIndex], [14, 15]);
});

test("defn name and params fields carry text and byte range", () => {
  const tree = parse(MODERN_DEFN);
  const root = tree.rootNode;
  assert.strictEqual(root.hasError, false);

  const defn = root.namedChildren[0];
  assert.strictEqual(defn.type, "defn");

  const name = defn.childForFieldName("name");
  assert.strictEqual(name.text, "add");
  assert.deepStrictEqual([name.startIndex, name.endIndex], [6, 9]);

  const params = defn.childForFieldName("params");
  assert.strictEqual(params.text, "(a b)");
  assert.deepStrictEqual([params.startIndex, params.endIndex], [10, 15]);
});

test("dotted list tail field points at the last expression", () => {
  const tree = parse(DOTTED_CALL);
  const root = tree.rootNode;
  assert.strictEqual(root.hasError, false);

  const call = root.namedChildren[0];
  assert.strictEqual(call.type, "call");
  assert.deepStrictEqual([call.startIndex, call.endIndex], [0, 9]);

  const tail = call.childForFieldName("tail");
  assert.strictEqual(tail.type, "identifier");
  assert.strictEqual(tail.text, "c");
  assert.deepStrictEqual([tail.startIndex, tail.endIndex], [7, 8]);
});
