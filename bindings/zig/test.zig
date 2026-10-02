const std = @import("std");
const testing = std.testing;

const ts = @import("tree_sitter");
const root = @import("tree-sitter-skript");
const Language = ts.Language;
const Parser = ts.Parser;

// `#| a #| b |# c |#` only closes at the matching depth, so the whole run is a
// single comment node and the form behind it must still parse as code.
const nested_comment = "#| outer #| inner |# tail |# 42";
const nested_comment_text = "#| outer #| inner |# tail |#";

// An unterminated opener owns the rest of the input instead of being rejected,
// so no live code behind it is ever highlighted as code.
const unterminated_comment = "(+ 1 2) #| never closed\n(+ 3 4)";
const unterminated_comment_text = "#| never closed\n(+ 3 4)";

const legacy_defn = "(defn (rgba r g) (@rgba r g))";
const modern_defn = "(defn add (a b) (+ a b))";
const dotted_call = "(a b . c)";

/// Handing the language to the parser is the ABI check: `setLanguage` fails
/// with `error.IncompatibleLanguage` unless the generated parser is inside the
/// runtime's supported range. The returned tree owns its native allocation and
/// the caller releases it with `destroy`; the parser is released here.
fn parse(lang: *const Language, source: []const u8) !*ts.Tree {
    const parser = Parser.create();
    defer parser.destroy();

    try parser.setLanguage(lang);
    return parser.parseString(source, null) orelse error.ParseReturnedNull;
}

fn text(source: []const u8, node: ts.Node) []const u8 {
    return source[node.startByte()..node.endByte()];
}

test "nested block comment spans the whole comment and keeps the following form" {
    const lang: *const Language = Language.fromRaw(root.language());
    defer lang.destroy();

    const tree = try parse(lang, nested_comment);
    defer tree.destroy();

    const file = tree.rootNode();
    try testing.expect(!file.hasError());
    try testing.expectEqual(@as(u32, 2), file.namedChildCount());

    const comment = file.namedChild(0).?;
    try testing.expectEqualStrings("block_comment", comment.kind());
    try testing.expectEqual(@as(u32, 0), comment.startByte());
    try testing.expectEqual(@as(u32, 28), comment.endByte());
    try testing.expectEqualStrings(nested_comment_text, text(nested_comment, comment));

    const following = file.namedChild(1).?;
    try testing.expectEqualStrings("num_lit", following.kind());
    try testing.expectEqual(@as(u32, 29), following.startByte());
    try testing.expectEqual(@as(u32, 31), following.endByte());
    try testing.expectEqualStrings("42", text(nested_comment, following));
}

test "unterminated block comment owns the rest of the input" {
    const lang: *const Language = Language.fromRaw(root.language());
    defer lang.destroy();

    const tree = try parse(lang, unterminated_comment);
    defer tree.destroy();

    const file = tree.rootNode();
    try testing.expect(!file.hasError());

    // Exactly two forms: the closed call, then the comment that swallowed the
    // second line. Nothing behind the opener reads as code.
    try testing.expectEqual(@as(u32, 2), file.namedChildCount());

    const call = file.namedChild(0).?;
    try testing.expectEqualStrings("call", call.kind());
    try testing.expectEqual(@as(u32, 0), call.startByte());
    try testing.expectEqual(@as(u32, 7), call.endByte());
    try testing.expectEqualStrings("(+ 1 2)", text(unterminated_comment, call));

    const comment = file.namedChild(1).?;
    try testing.expectEqualStrings("block_comment", comment.kind());
    try testing.expectEqual(@as(u32, 8), comment.startByte());
    try testing.expectEqual(@as(u32, 31), comment.endByte());
    try testing.expectEqualStrings(unterminated_comment_text, text(unterminated_comment, comment));
}

test "legacy defn params carry text and byte range" {
    const lang: *const Language = Language.fromRaw(root.language());
    defer lang.destroy();

    const tree = try parse(lang, legacy_defn);
    defer tree.destroy();

    const file = tree.rootNode();
    try testing.expect(!file.hasError());
    try testing.expectEqual(@as(u32, 1), file.namedChildCount());

    const defn = file.namedChild(0).?;
    try testing.expectEqualStrings("defn", defn.kind());
    // The legacy shape carries no `name` field; the bound name is the first
    // arglist element, which is what the highlights query keys on.
    try testing.expect(defn.childByFieldName("name") == null);

    const params = defn.childByFieldName("params").?;
    try testing.expectEqualStrings("list", params.kind());
    try testing.expectEqual(@as(u32, 6), params.startByte());
    try testing.expectEqual(@as(u32, 16), params.endByte());
    try testing.expectEqualStrings("(rgba r g)", text(legacy_defn, params));
    try testing.expectEqual(@as(u32, 3), params.namedChildCount());

    const name = params.namedChild(0).?;
    try testing.expectEqualStrings("identifier", name.kind());
    try testing.expectEqualStrings("rgba", text(legacy_defn, name));
    try testing.expectEqual(@as(u32, 7), name.startByte());
    try testing.expectEqual(@as(u32, 11), name.endByte());

    const first_param = params.namedChild(1).?;
    try testing.expectEqualStrings("r", text(legacy_defn, first_param));
    try testing.expectEqual(@as(u32, 12), first_param.startByte());
    try testing.expectEqual(@as(u32, 13), first_param.endByte());
}

test "defn name and params fields carry text and byte range" {
    const lang: *const Language = Language.fromRaw(root.language());
    defer lang.destroy();

    const tree = try parse(lang, modern_defn);
    defer tree.destroy();

    const file = tree.rootNode();
    try testing.expect(!file.hasError());

    const defn = file.namedChild(0).?;
    try testing.expectEqualStrings("defn", defn.kind());

    const name = defn.childByFieldName("name").?;
    try testing.expectEqualStrings("add", text(modern_defn, name));
    try testing.expectEqual(@as(u32, 6), name.startByte());
    try testing.expectEqual(@as(u32, 9), name.endByte());

    const params = defn.childByFieldName("params").?;
    try testing.expectEqualStrings("(a b)", text(modern_defn, params));
    try testing.expectEqual(@as(u32, 10), params.startByte());
    try testing.expectEqual(@as(u32, 15), params.endByte());
}

test "dotted list tail field points at the last expression" {
    const lang: *const Language = Language.fromRaw(root.language());
    defer lang.destroy();

    const tree = try parse(lang, dotted_call);
    defer tree.destroy();

    const file = tree.rootNode();
    try testing.expect(!file.hasError());

    const call = file.namedChild(0).?;
    try testing.expectEqualStrings("call", call.kind());
    try testing.expectEqual(@as(u32, 0), call.startByte());
    try testing.expectEqual(@as(u32, 9), call.endByte());

    const tail = call.childByFieldName("tail").?;
    try testing.expectEqualStrings("identifier", tail.kind());
    try testing.expectEqualStrings("c", text(dotted_call, tail));
    try testing.expectEqual(@as(u32, 7), tail.startByte());
    try testing.expectEqual(@as(u32, 8), tail.endByte());
}
