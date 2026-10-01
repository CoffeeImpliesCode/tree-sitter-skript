package tree_sitter_skript_test

import (
	"testing"

	tree_sitter "github.com/tree-sitter/go-tree-sitter"
	tree_sitter_skript "github.com/coffeeimpliescode/tree-sitter-skript/bindings/go"
)

// The parser is generated as ABI 15. A runtime that does not accept that
// version makes SetLanguage fail, which NewLanguage alone does not reveal -
// this test used to pass green against a runtime capped at ABI 14.
func TestCanLoadGrammar(t *testing.T) {
	language := tree_sitter.NewLanguage(tree_sitter_skript.Language())
	if language == nil {
		t.Fatal("Error loading Skript grammar")
	}

	parser := tree_sitter.NewParser()
	defer parser.Close()
	if err := parser.SetLanguage(language); err != nil {
		t.Fatalf("SetLanguage: %v", err)
	}
}

// A binding that loads is not yet a binding that parses: assert the tree a
// consumer actually gets for the two shapes skript uses in practice - the
// legacy `(defn (NAME ARGS) ...)` and a dotted list.
func TestParsesSkript(t *testing.T) {
	language := tree_sitter.NewLanguage(tree_sitter_skript.Language())
	parser := tree_sitter.NewParser()
	defer parser.Close()
	if err := parser.SetLanguage(language); err != nil {
		t.Fatalf("SetLanguage: %v", err)
	}

	cases := []struct {
		name     string
		source   string
		expected string
	}{
		{
			name:     "legacy defn",
			source:   "(defn (fib n) n)",
			expected: "(source_file (defn params: (list (identifier) (identifier)) (identifier)))",
		},
		{
			name:     "dotted list",
			source:   "(a . b)",
			expected: "(source_file (call (identifier) tail: (identifier)))",
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			tree := parser.Parse([]byte(tc.source), nil)
			if tree == nil {
				t.Fatal("Parse returned no tree")
			}
			defer tree.Close()
			if got := tree.RootNode().ToSexp(); got != tc.expected {
				t.Errorf("tree mismatch\n got: %s\nwant: %s", got, tc.expected)
			}
		})
	}
}