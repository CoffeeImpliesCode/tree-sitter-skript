// Consumer-level contract tests for the Go binding. Everything here goes
// through the public go-tree-sitter API against real Skript source and reads
// the same things a real consumer reads - node kinds, field names, byte
// ranges and the text those ranges cover. Nothing here inspects the C sources
// or the parser tables.
package tree_sitter_skript_test

import (
	"testing"

	tree_sitter_skript "github.com/coffeeimpliescode/tree-sitter-skript/bindings/go"
	tree_sitter "github.com/tree-sitter/go-tree-sitter"
)

// parse runs source through the grammar.
//
// SetLanguage is the ABI gate: the generated parser is ABI 15 and the runtime
// rejects anything outside MIN_COMPATIBLE_LANGUAGE_VERSION..=LANGUAGE_VERSION,
// so every case below is also the load check. A separate "can I load it?"
// test would only restate the constructor.
func parse(t *testing.T, source string) *tree_sitter.Node {
	t.Helper()

	language := tree_sitter.NewLanguage(tree_sitter_skript.Language())
	parser := tree_sitter.NewParser()
	t.Cleanup(parser.Close)
	if err := parser.SetLanguage(language); err != nil {
		t.Fatalf("SetLanguage: %v", err)
	}

	tree := parser.Parse([]byte(source), nil)
	if tree == nil {
		t.Fatal("Parse returned no tree")
	}
	t.Cleanup(tree.Close)

	return tree.RootNode()
}

func namedChild(t *testing.T, node *tree_sitter.Node, index uint) *tree_sitter.Node {
	t.Helper()

	child := node.NamedChild(index)
	if child == nil {
		t.Fatalf("%s has no named child %d: %s", node.Kind(), index, node.ToSexp())
	}
	return child
}

func fieldNode(t *testing.T, node *tree_sitter.Node, name string) *tree_sitter.Node {
	t.Helper()

	child := node.ChildByFieldName(name)
	if child == nil {
		t.Fatalf("%s has no %q field: %s", node.Kind(), name, node.ToSexp())
	}
	return child
}

// assertNode pins what a consumer reads off a node: its kind and its exact
// byte range in the source it was parsed from.
func assertNode(t *testing.T, node *tree_sitter.Node, source []byte, wantKind string, wantStart, wantEnd uint) {
	t.Helper()

	if got := node.Kind(); got != wantKind {
		t.Fatalf("kind = %q, want %q: %s", got, wantKind, node.ToSexp())
	}
	if got := node.StartByte(); got != wantStart {
		t.Errorf("%s start byte = %d, want %d", wantKind, got, wantStart)
	}
	if got := node.EndByte(); got != wantEnd {
		t.Errorf("%s end byte = %d, want %d", wantKind, got, wantEnd)
	}
}

// liveChild returns the first named child that is not a comment. Where the
// grammar gives a form no field, this is the node a consumer lands on, so a
// comment must never take that slot.
func liveChild(t *testing.T, node *tree_sitter.Node) *tree_sitter.Node {
	t.Helper()

	for i := uint(0); i < node.NamedChildCount(); i++ {
		child := node.NamedChild(i)
		if child == nil {
			t.Fatalf("%s has no named child %d: %s", node.Kind(), i, node.ToSexp())
		}
		switch child.Kind() {
		case "block_comment", "datum_comment", "line_comment":
			continue
		}
		return child
	}

	t.Fatalf("%s has no child that is not a comment: %s", node.Kind(), node.ToSexp())
	return nil
}

func assertText(t *testing.T, node *tree_sitter.Node, source []byte, want string) {
	t.Helper()

	if got := node.Utf8Text(source); got != want {
		t.Errorf("%s text = %q, want %q", node.Kind(), got, want)
	}
}

// A binding that loads is not yet a binding that parses. The legacy
// `(defn (NAME ARGS) ...)` spelling has no `name` field: the head of the
// arglist IS the name, and everything after the arglist is the body.
func TestLegacyDefnExposesItsArglistAndBody(t *testing.T) {
	const text = "(defn (fib n) n)"
	source := []byte(text)
	root := parse(t, text)

	if root.Kind() != "source_file" {
		t.Fatalf("root kind = %q, want source_file", root.Kind())
	}
	if got := root.NamedChildCount(); got != 1 {
		t.Fatalf("named children = %d, want 1: %s", got, root.ToSexp())
	}

	defn := namedChild(t, root, 0)
	assertNode(t, defn, source, "defn", 0, uint(len(text)))
	if name := defn.ChildByFieldName("name"); name != nil {
		t.Errorf("legacy defn reports a name field %s; the arglist head is the name", name.ToSexp())
	}

	params := fieldNode(t, defn, "params")
	assertNode(t, params, source, "list", 6, 13)
	if got := params.NamedChildCount(); got != 2 {
		t.Fatalf("params named children = %d, want 2: %s", got, params.ToSexp())
	}
	assertNode(t, namedChild(t, params, 0), source, "identifier", 7, 10)
	assertNode(t, namedChild(t, params, 1), source, "identifier", 11, 12)
	assertText(t, namedChild(t, params, 0), source, "fib")

	// The body is the only remaining named child, and it keeps its own range.
	assertNode(t, namedChild(t, defn, 1), source, "identifier", 14, 15)
	assertText(t, namedChild(t, defn, 1), source, "n")

	if root.HasError() {
		t.Errorf("tree has errors: %s", root.ToSexp())
	}
}

// The dotted list the reader spells `(a . b)`: the tail is reachable through a
// field rather than by position, so a consumer does not have to guess which
// child is which.
func TestDottedListExposesItsTailField(t *testing.T) {
	const text = "(a . b)"
	source := []byte(text)
	root := parse(t, text)

	call := namedChild(t, root, 0)
	assertNode(t, call, source, "call", 0, uint(len(text)))
	assertNode(t, namedChild(t, call, 0), source, "identifier", 1, 2)

	tail := fieldNode(t, call, "tail")
	assertNode(t, tail, source, "identifier", 5, 6)
	assertText(t, tail, source, "b")

	if root.HasError() {
		t.Errorf("tree has errors: %s", root.ToSexp())
	}
}

// The reader counts nesting depth when it lexes `#| ... |#`, so the inner
// `|#` must not close the comment. A consumer needs the node to cover every
// delimiter - otherwise the comment's text (what a highlighter prints) stops
// halfway - and the form after it must still parse.
func TestNestedBlockCommentKeepsItsWholeSpan(t *testing.T) {
	const comment = "#| outer #| inner |# still comment |#"
	const text = comment + " 42"
	source := []byte(text)
	root := parse(t, text)

	if got := root.NamedChildCount(); got != 2 {
		t.Fatalf("named children = %d, want 2 (comment then form): %s", got, root.ToSexp())
	}

	// The span ends at the closer, before trailing whitespace.
	block := namedChild(t, root, 0)
	assertNode(t, block, source, "block_comment", 0, uint(len(comment)))
	assertText(t, block, source, comment)
	if !block.IsExtra() {
		t.Error("block_comment is not an extra, so it can take a form's value slot")
	}

	// ... and the form after the comment is a real node with its own range.
	form := namedChild(t, root, 1)
	assertNode(t, form, source, "num_lit", uint(len(comment)+1), uint(len(text)))
	assertText(t, form, source, "42")

	if root.HasError() {
		t.Errorf("tree has errors: %s", root.ToSexp())
	}
}

// An unterminated `#|` owns the rest of the file. That is the editor case: a
// tree still exists, it covers the source the author has typed so far, and the
// form the comment swallowed is inside the comment rather than beside it.
func TestUnterminatedBlockCommentOwnsTheRestOfTheFile(t *testing.T) {
	const text = "#| outer #| inner |# (+ 1 2)"
	source := []byte(text)
	root := parse(t, text)

	// One child only: `(+ 1 2)` was consumed by the comment, not parsed.
	if got := root.NamedChildCount(); got != 1 {
		t.Fatalf("named children = %d, want 1: %s", got, root.ToSexp())
	}

	block := namedChild(t, root, 0)
	assertNode(t, block, source, "block_comment", 0, uint(len(text)))
	if got := root.EndByte(); got != uint(len(text)) {
		t.Errorf("root end byte = %d, want %d", got, len(text))
	}
	if root.HasError() {
		t.Errorf("tree has errors: %s", root.ToSexp())
	}
}

// `#;FORM` throws the form away (Reader.zig), so the node is opaque: it covers
// the `#;` and the whole discarded form and has no children to mislead a
// consumer into editing code that is not there. The form AFTER it is real.
func TestDatumCommentIsOpaqueButTheNextFormStaysLive(t *testing.T) {
	const text = "#;(1 2) 3"
	source := []byte(text)
	root := parse(t, text)

	if got := root.NamedChildCount(); got != 2 {
		t.Fatalf("named children = %d, want 2 (comment then form): %s", got, root.ToSexp())
	}

	datum := namedChild(t, root, 0)
	assertNode(t, datum, source, "datum_comment", 0, 7)
	assertText(t, datum, source, "#;(1 2)")
	if got := datum.NamedChildCount(); got != 0 {
		t.Errorf("datum_comment has %d children, want 0 - the reader discards the form whole", got)
	}
	if !datum.IsExtra() {
		t.Error("datum_comment is not an extra, so it can take a form's value slot")
	}

	assertNode(t, namedChild(t, root, 1), source, "num_lit", 8, 9)
}

// Byte offsets are byte offsets, not character offsets. `é` and `ö` are two
// bytes each, so every range after them shifts by one - a binding that
// reported characters instead would land one short on all three nodes.
func TestByteRangesSurviveMultibyteSource(t *testing.T) {
	const text = "héllo #;wörld ok"
	source := []byte(text)
	root := parse(t, text)

	if got := root.NamedChildCount(); got != 3 {
		t.Fatalf("named children = %d, want 3: %s", got, root.ToSexp())
	}

	head := namedChild(t, root, 0)
	assertNode(t, head, source, "identifier", 0, 6)
	assertText(t, head, source, "héllo")

	assertNode(t, namedChild(t, root, 1), source, "datum_comment", 7, 15)
	assertNode(t, namedChild(t, root, 2), source, "identifier", 16, 18)
}

// Comments are extras, so they cannot land in the slot a consumer reads as a
// value or as a quoted form. If one ever did, `value:`/`tail:`-style queries
// and every editor "rename this form" would follow the comment instead.
func TestCommentsDoNotOccupyFormValueSlots(t *testing.T) {
	cases := []struct {
		name     string
		source   string
		owner    string
		field    string // empty when the grammar gives the form no field
		wantKind string
		wantText string
	}{
		{
			name:     "def value",
			source:   `(def a #| c |# 1)`,
			owner:    "def",
			field:    "value",
			wantKind: "num_lit",
			wantText: "1",
		},
		{
			name:     "map value",
			source:   `:{ :k #| c |# 1 }`,
			owner:    "map_lit",
			field:    "value",
			wantKind: "num_lit",
			wantText: "1",
		},
		{
			name:     "quoted form",
			source:   `' #| c |# x`,
			owner:    "quote_lit",
			wantKind: "identifier",
			wantText: "x",
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			source := []byte(tc.source)
			root := parse(t, tc.source)

			owner := namedChild(t, root, 0)
			if owner.Kind() != tc.owner {
				t.Fatalf("root child kind = %q, want %q: %s", owner.Kind(), tc.owner, root.ToSexp())
			}

			// A field when the grammar names the slot, otherwise the first
			// child that is not the comment.
			var value *tree_sitter.Node
			if tc.field != "" {
				value = fieldNode(t, owner, tc.field)
			} else {
				value = liveChild(t, owner)
			}

			if got := value.Kind(); got != tc.wantKind {
				t.Fatalf("%s value kind = %q, want %q: %s", tc.owner, got, tc.wantKind, owner.ToSexp())
			}
			assertText(t, value, source, tc.wantText)

			if root.HasError() {
				t.Errorf("tree has errors: %s", root.ToSexp())
			}
		})
	}
}
