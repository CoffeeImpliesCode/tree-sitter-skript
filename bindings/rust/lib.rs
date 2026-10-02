//! This crate provides Skript language support for the [tree-sitter] parsing library.
//!
//! Typically, you will use the [`LANGUAGE`] constant to add this language to a
//! tree-sitter [`Parser`], and then use the parser to parse some code:
//!
//! ```
//! let code = r#"
//! "#;
//! let mut parser = tree_sitter::Parser::new();
//! let language = tree_sitter_skript::LANGUAGE;
//! parser
//!     .set_language(&language.into())
//!     .expect("Error loading Skript parser");
//! let tree = parser.parse(code, None).unwrap();
//! assert!(!tree.root_node().has_error());
//! ```
//!
//! [`Parser`]: https://docs.rs/tree-sitter/0.26.8/tree_sitter/struct.Parser.html
//! [tree-sitter]: https://tree-sitter.github.io/

use tree_sitter_language::LanguageFn;

extern "C" {
    fn tree_sitter_skript() -> *const ();
}

/// The tree-sitter [`LanguageFn`] for this grammar.
pub const LANGUAGE: LanguageFn = unsafe { LanguageFn::from_raw(tree_sitter_skript) };

/// The content of the [`node-types.json`] file for this grammar.
///
/// [`node-types.json`]: https://tree-sitter.github.io/tree-sitter/using-parsers/6-static-node-types
pub const NODE_TYPES: &str = include_str!("../../src/node-types.json");

#[cfg(with_highlights_query)]
/// The syntax highlighting query for this grammar.
pub const HIGHLIGHTS_QUERY: &str = include_str!("../../queries/highlights.scm");

#[cfg(with_injections_query)]
/// The language injection query for this grammar.
pub const INJECTIONS_QUERY: &str = include_str!("../../queries/injections.scm");

#[cfg(with_locals_query)]
/// The local variable query for this grammar.
pub const LOCALS_QUERY: &str = include_str!("../../queries/locals.scm");

#[cfg(with_tags_query)]
/// The symbol tagging query for this grammar.
pub const TAGS_QUERY: &str = include_str!("../../queries/tags.scm");

#[cfg(test)]
mod tests {
    use super::LANGUAGE;
    use tree_sitter::{Node, Parser, Tree};

    /// Runs `source` through the grammar.
    ///
    /// `set_language` is the ABI gate: the generated parser is ABI 15 and the
    /// runtime rejects anything outside `MIN_COMPATIBLE_LANGUAGE_VERSION..=
    /// LANGUAGE_VERSION`, so every test below is also the load check. A
    /// separate "can I load it?" test would only restate the constructor.
    fn parse(source: &str) -> Tree {
        let mut parser = Parser::new();
        parser
            .set_language(&LANGUAGE.into())
            .expect("Skript must load into this runtime's tree-sitter");
        parser
            .parse(source, None)
            .expect("parse() only returns None when the parse was cancelled")
    }

    fn named_child<'tree>(node: &Node<'tree>, index: u32) -> Node<'tree> {
        node.named_child(index)
            .unwrap_or_else(|| panic!("{node:?} has no named child {index}"))
    }

    fn field<'tree>(node: &Node<'tree>, name: &str) -> Node<'tree> {
        node.child_by_field_name(name)
            .unwrap_or_else(|| panic!("{node:?} has no {name:?} field"))
    }

    /// The first named child that is not a comment. Where the grammar gives a
    /// form no field, this is the node a consumer lands on, so a comment must
    /// never take that slot.
    fn live_child<'tree>(node: &Node<'tree>) -> Node<'tree> {
        for index in 0..node.named_child_count() {
            let child = node.named_child(index as u32).expect("index is in range");
            if matches!(
                child.kind(),
                "block_comment" | "datum_comment" | "line_comment"
            ) {
                continue;
            }
            return child;
        }
        panic!("{node:?} has no child that is not a comment");
    }

    fn text<'a>(node: Node<'_>, source: &'a str) -> &'a str {
        node.utf8_text(source.as_bytes())
            .unwrap_or_else(|err| panic!("{node:?} does not cover valid UTF-8: {err}"))
    }

    /// Pins what a consumer reads off a node: its kind and its exact byte
    /// range in the source it was parsed from.
    fn assert_node(
        node: Node<'_>,
        source: &str,
        want_kind: &str,
        want_start: usize,
        want_end: usize,
    ) {
        assert_eq!(node.kind(), want_kind, "unexpected node: {node:?}");
        assert_eq!(
            (node.start_byte(), node.end_byte()),
            (want_start, want_end),
            "{want_kind} has the wrong range in {source:?}"
        );
    }

    fn assert_text(node: Node<'_>, source: &str, want: &str) {
        assert_eq!(text(node, source), want, "unexpected {} text", node.kind());
    }

    /// A binding that loads is not yet a binding that parses. The legacy
    /// `(defn (NAME ARGS) ...)` spelling has no `name` field: the head of
    /// the arglist IS the name, and everything after the arglist is the body.
    #[test]
    fn legacy_defn_exposes_its_arglist_and_body() {
        let source = "(defn (fib n) n)";
        let tree = parse(source);
        let root = tree.root_node();

        assert_eq!(root.kind(), "source_file", "{root:?}");
        assert_eq!(root.named_child_count(), 1, "{root:?}");

        let defn = named_child(&root, 0);
        assert_node(defn, source, "defn", 0, source.len());
        assert!(
            defn.child_by_field_name("name").is_none(),
            "legacy defn reports a name field: {defn:?}"
        );

        let params = field(&defn, "params");
        assert_node(params, source, "list", 6, 13);
        assert_eq!(params.named_child_count(), 2, "{params:?}");
        assert_node(named_child(&params, 0), source, "identifier", 7, 10);
        assert_text(named_child(&params, 0), source, "fib");
        assert_node(named_child(&params, 1), source, "identifier", 11, 12);

        // The body is the only remaining named child, and keeps its own range.
        let body = named_child(&defn, 1);
        assert_node(body, source, "identifier", 14, 15);
        assert_text(body, source, "n");

        assert!(!root.has_error(), "{root:?}");
    }

    /// The dotted list the reader spells `(a . b)`: the tail is reachable
    /// through a field rather than by position, so a consumer does not have to
    /// guess which child is which.
    #[test]
    fn dotted_list_exposes_its_tail_field() {
        let source = "(a . b)";
        let tree = parse(source);
        let root = tree.root_node();

        let call = named_child(&root, 0);
        assert_node(call, source, "call", 0, source.len());
        assert_node(named_child(&call, 0), source, "identifier", 1, 2);

        let tail = field(&call, "tail");
        assert_node(tail, source, "identifier", 5, 6);
        assert_text(tail, source, "b");

        assert!(!root.has_error(), "{root:?}");
    }

    /// The reader counts nesting depth when it lexes `#| ... |#`, so the inner
    /// `|#` must not close the comment. A consumer needs the node to cover
    /// every delimiter - otherwise the comment's text (what a highlighter
    /// prints) stops halfway - and the form after it must still parse.
    #[test]
    fn nested_block_comment_keeps_its_whole_span() {
        let comment = "#| outer #| inner |# still comment |#";
        let source = format!("{comment} 42");
        let tree = parse(&source);
        let root = tree.root_node();

        assert_eq!(root.named_child_count(), 2, "{root:?}");

        // The span ends at the closer, before trailing whitespace.
        let form_start = source.find("42").expect("the form is in the source");
        let block = named_child(&root, 0);
        assert_node(block, &source, "block_comment", 0, comment.len());
        assert_text(block, &source, comment);
        assert!(block.is_extra(), "{block:?} is not an extra");

        let form = named_child(&root, 1);
        assert_node(form, &source, "num_lit", form_start, source.len());
        assert_text(form, &source, "42");

        assert!(!root.has_error(), "{root:?}");
    }

    /// An unterminated `#|` owns the rest of the file. That is the editor
    /// case: a tree still exists, it covers the source the author has typed so
    /// far, and the form the comment swallowed is inside the comment rather
    /// than beside it.
    #[test]
    fn unterminated_block_comment_owns_the_rest_of_the_file() {
        let source = "#| outer #| inner |# (+ 1 2)";
        let tree = parse(source);
        let root = tree.root_node();

        // One child only: `(+ 1 2)` was consumed by the comment, not parsed.
        assert_eq!(root.named_child_count(), 1, "{root:?}");

        let block = named_child(&root, 0);
        assert_node(block, source, "block_comment", 0, source.len());
        assert_eq!(root.end_byte(), source.len(), "{root:?}");
        assert!(!root.has_error(), "{root:?}");
    }

    /// `#;FORM` throws the form away (Reader.zig), so the node is opaque: it
    /// covers the `#;` and the whole discarded form and has no children to
    /// mislead a consumer into editing code that is not there. The form AFTER
    /// it is real.
    #[test]
    fn datum_comment_is_opaque_but_the_next_form_stays_live() {
        let source = "#;(1 2) 3";
        let tree = parse(source);
        let root = tree.root_node();

        assert_eq!(root.named_child_count(), 2, "{root:?}");

        let datum = named_child(&root, 0);
        assert_node(datum, source, "datum_comment", 0, 7);
        assert_text(datum, source, "#;(1 2)");
        assert_eq!(
            datum.named_child_count(),
            0,
            "the reader discards the form whole: {datum:?}"
        );
        assert!(datum.is_extra(), "{datum:?} is not an extra");

        assert_node(named_child(&root, 1), source, "num_lit", 8, 9);
    }

    /// Byte offsets are byte offsets, not character offsets. `é` and `ö` are
    /// two bytes each, so every range after them shifts by one - a binding
    /// reporting characters would land one short on all three nodes.
    #[test]
    fn byte_ranges_survive_multibyte_source() {
        let source = "héllo #;wörld ok";
        let tree = parse(source);
        let root = tree.root_node();

        assert_eq!(root.named_child_count(), 3, "{root:?}");

        let head = named_child(&root, 0);
        assert_node(head, source, "identifier", 0, 6);
        assert_text(head, source, "héllo");

        assert_node(named_child(&root, 1), source, "datum_comment", 7, 15);
        assert_node(named_child(&root, 2), source, "identifier", 16, 18);
    }

    /// Comments are extras, so they cannot land in the slot a consumer reads
    /// as a value or as a quoted form. If one ever did, `value:`-style queries
    /// and every editor "rename this form" would follow the comment instead.
    #[test]
    fn comments_do_not_occupy_form_value_slots() {
        // Case name, source, owning form, field (empty when the form has
        // none), expected value kind, expected value text.
        let cases = [
            (
                "def value",
                "(def a #| c |# 1)",
                "def",
                "value",
                "num_lit",
                "1",
            ),
            (
                "map value",
                ":{ :k #| c |# 1 }",
                "map_lit",
                "value",
                "num_lit",
                "1",
            ),
            (
                "quoted form",
                "' #| c |# x",
                "quote_lit",
                "",
                "identifier",
                "x",
            ),
        ];

        for (name, source, owner_kind, field_name, want_kind, want_text) in cases {
            let tree = parse(source);
            let root = tree.root_node();

            let owner = named_child(&root, 0);
            assert_eq!(owner.kind(), owner_kind, "{name}: {root:?}");

            // A field when the grammar names the slot, otherwise the first
            // child that is not the comment.
            let value = if field_name.is_empty() {
                live_child(&owner)
            } else {
                field(&owner, field_name)
            };

            assert_eq!(value.kind(), want_kind, "{name}: {}", owner.to_sexp());
            assert_text(value, source, want_text);
            assert!(!root.has_error(), "{name}: {root:?}");
        }
    }

    /// The shape all three NUL cases share: the comment owns its whole span,
    /// is a childless extra, and the `42` after it is a real form with its
    /// own range.
    fn assert_nul_comment_then_42(
        source: &str,
        want_kind: &str,
        want_comment: &str,
        want_end: usize,
    ) {
        let tree = parse(source);
        let root = tree.root_node();

        // The comment and the form, and nothing else.
        assert_eq!(root.named_child_count(), 2, "{root:?}");
        assert!(!root.has_error(), "{root:?}");

        let comment = named_child(&root, 0);
        assert_node(comment, source, want_kind, 0, want_end);
        assert_text(comment, source, want_comment);
        assert_eq!(comment.named_child_count(), 0, "{comment:?}");
        assert!(comment.is_extra(), "{comment:?} is not an extra");

        let form = named_child(&root, 1);
        assert_node(form, source, "num_lit", source.len() - 2, source.len());
        assert_text(form, source, "42");
    }

    #[test]
    fn discarded_string_honors_escaped_quotes() {
        let source = r#"#;"a\" b" 42"#;
        let tree = parse(source);
        let root = tree.root_node();
        assert!(!root.has_error(), "{root:?}");
        assert_eq!(root.named_child_count(), 2, "{root:?}");

        let comment = named_child(&root, 0);
        assert_node(comment, source, "datum_comment", 0, 9);
        assert_text(comment, source, r#"#;"a\" b""#);
        assert!(comment.is_extra(), "{comment:?}");
        assert_eq!(comment.named_child_count(), 0, "{comment:?}");
        assert_node(named_child(&root, 1), source, "num_lit", 10, 12);
    }

    /// A NUL is an ordinary byte to the reader: `Token.zig` asks whether the
    /// offset is still inside the source, never whether the byte is zero, so a
    /// NUL closes nothing and ends nothing. `\0` below is the only NUL in this
    /// file; the source is built here, never committed to the corpus.
    #[test]
    fn nul_inside_a_nested_block_comment_stays_comment() {
        assert_nul_comment_then_42(
            "#| a\0b #| c\0d |# e\0f |# 42",
            "block_comment",
            "#| a\0b #| c\0d |# e\0f |#",
            23,
        );
    }

    /// Same contract for the string a datum comment throws away: the whole
    /// `"..."` is part of the discarded span, NUL included.
    #[test]
    fn nul_inside_a_discarded_string_stays_in_the_datum_comment() {
        assert_nul_comment_then_42("#;\"a\0b\" 42", "datum_comment", "#;\"a\0b\"", 7);
    }

    /// And for an identifier body, which the reader runs to the next
    /// whitespace or delimiter and never to a zero byte.
    #[test]
    fn nul_inside_a_discarded_identifier_stays_in_the_datum_comment() {
        assert_nul_comment_then_42("#;a\0b 42", "datum_comment", "#;a\0b", 5);
    }

    /// The blank skipper between `#;` and its value must read a line comment
    /// the same way the reader does: the NUL inside `;a\0b` is ordinary text,
    /// and the discarded form is the `1` on the next line, so the datum owns
    /// the comment too.
    #[test]
    fn nul_inside_a_skipped_line_comment_stays_inside_the_datum_comment() {
        assert_nul_comment_then_42("#; ;a\0b\n1 42", "datum_comment", "#; ;a\0b\n1", 9);
    }

    /// The value's own first byte: an identifier may start with any byte the
    /// reader does not reserve, and zero is not among the reserved ones, so
    /// the atom loop must not stop before it has read one.
    #[test]
    fn nul_at_the_start_of_a_discarded_identifier_stays_in_the_datum_comment() {
        assert_nul_comment_then_42("#;\0a 42", "datum_comment", "#;\0a", 4);
    }
}
