from unittest import TestCase

from tree_sitter import Language, Parser, Tree
import tree_sitter_skript

# `#| a #| b |# c |#` only closes at the matching depth, so the whole run is a
# single comment node and the form behind it must still parse as code.
NESTED_COMMENT = b"#| outer #| inner |# tail |# 42"
NESTED_COMMENT_TEXT = b"#| outer #| inner |# tail |#"

# An unterminated opener owns the rest of the input instead of being rejected,
# so no live code behind it is ever highlighted as code.
UNTERMINATED_COMMENT = b"(+ 1 2) #| never closed\n(+ 3 4)"
UNTERMINATED_COMMENT_TEXT = b"#| never closed\n(+ 3 4)"

LEGACY_DEFN = b"(defn (rgba r g) (@rgba r g))"
MODERN_DEFN = b"(defn add (a b) (+ a b))"
DOTTED_CALL = b"(a b . c)"


class TestSkriptParser(TestCase):
    def setUp(self):
        self.language = Language(tree_sitter_skript.language())

    def parse(self, source: bytes) -> Tree:
        # Handing the language to a Parser is the ABI check: py-tree-sitter
        # raises ValueError unless the generated parser is inside the runtime's
        # supported range. The returned Tree owns the native TSTree and releases
        # it with its last reference, so callers hold it while reading nodes.
        return Parser(self.language).parse(source)

    def test_nested_block_comment_spans_whole_comment_and_keeps_next_form(self):
        tree = self.parse(NESTED_COMMENT)
        root = tree.root_node
        self.assertFalse(root.has_error)

        children = root.named_children
        self.assertEqual(
            ["block_comment", "num_lit"], [child.type for child in children]
        )

        comment, following = children
        self.assertEqual(NESTED_COMMENT_TEXT, comment.text)
        self.assertEqual((0, 28), comment.byte_range)
        self.assertEqual(b"42", following.text)
        self.assertEqual((29, 31), following.byte_range)

    def test_unterminated_block_comment_owns_the_rest_of_the_input(self):
        tree = self.parse(UNTERMINATED_COMMENT)
        root = tree.root_node
        self.assertFalse(root.has_error)

        # Exactly two forms: the closed call, then the comment that swallowed
        # the second line. Nothing behind the opener reads as code.
        children = root.named_children
        self.assertEqual(["call", "block_comment"], [child.type for child in children])

        call, comment = children
        self.assertEqual(b"(+ 1 2)", call.text)
        self.assertEqual((0, 7), call.byte_range)
        self.assertEqual(UNTERMINATED_COMMENT_TEXT, comment.text)
        self.assertEqual((8, 31), comment.byte_range)

    def test_legacy_defn_params_carry_text_and_byte_range(self):
        tree = self.parse(LEGACY_DEFN)
        root = tree.root_node
        self.assertFalse(root.has_error)

        defn = root.named_children[0]
        self.assertEqual("defn", defn.type)
        # The legacy shape carries no name: field; the bound name is the first
        # arglist element, which is what the highlights query keys on.
        self.assertIsNone(defn.child_by_field_name("name"))

        params = defn.child_by_field_name("params")
        self.assertEqual("list", params.type)
        self.assertEqual(b"(rgba r g)", params.text)
        self.assertEqual((6, 16), params.byte_range)

        names = params.named_children
        self.assertEqual([b"rgba", b"r", b"g"], [name.text for name in names])
        self.assertEqual((7, 11), names[0].byte_range)
        self.assertEqual((12, 13), names[1].byte_range)
        self.assertEqual((14, 15), names[2].byte_range)

    def test_defn_name_and_params_fields_carry_text_and_byte_range(self):
        tree = self.parse(MODERN_DEFN)
        root = tree.root_node
        self.assertFalse(root.has_error)

        defn = root.named_children[0]
        self.assertEqual("defn", defn.type)

        name = defn.child_by_field_name("name")
        self.assertEqual(b"add", name.text)
        self.assertEqual((6, 9), name.byte_range)

        params = defn.child_by_field_name("params")
        self.assertEqual(b"(a b)", params.text)
        self.assertEqual((10, 15), params.byte_range)

    def test_dotted_list_tail_field_points_at_the_last_expression(self):
        tree = self.parse(DOTTED_CALL)
        root = tree.root_node
        self.assertFalse(root.has_error)

        call = root.named_children[0]
        self.assertEqual("call", call.type)
        self.assertEqual((0, 9), call.byte_range)

        tail = call.child_by_field_name("tail")
        self.assertEqual("identifier", tail.type)
        self.assertEqual(b"c", tail.text)
        self.assertEqual((7, 8), tail.byte_range)
