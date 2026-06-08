package tree_sitter_skript_test

import (
	"testing"

	tree_sitter "github.com/tree-sitter/go-tree-sitter"
	tree_sitter_skript "github.com/coffeeimpliescode/tree-sitter-skript/bindings/go"
)

func TestCanLoadGrammar(t *testing.T) {
	language := tree_sitter.NewLanguage(tree_sitter_skript.Language())
	if language == nil {
		t.Errorf("Error loading Skript grammar")
	}
}
