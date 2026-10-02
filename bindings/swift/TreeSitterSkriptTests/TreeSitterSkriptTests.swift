import XCTest
import SwiftTreeSitter
import TreeSitterSkript

// `#| a #| b |# c |#` only closes at the matching depth, so the whole run is a
// single comment node and the form behind it must still parse as code.
private let nestedComment = "#| outer #| inner |# tail |# 42"
private let nestedCommentText = "#| outer #| inner |# tail |#"

// An unterminated opener owns the rest of the input instead of being rejected,
// so no live code behind it is ever highlighted as code.
private let unterminatedComment = "(+ 1 2) #| never closed\n(+ 3 4)"
private let unterminatedCommentText = "#| never closed\n(+ 3 4)"

private let legacyDefn = "(defn (rgba r g) (@rgba r g))"
private let modernDefn = "(defn add (a b) (+ a b))"
private let dottedCall = "(a b . c)"

final class TreeSitterSkriptTests: XCTestCase {
    /// Handing the language to the parser is the ABI check: `setLanguage`
    /// throws unless the generated parser is inside the runtime's supported
    /// range. SwiftTreeSitter parses UTF-16LE and reference-counts both the
    /// parser and the tree, and a `Node` retains its tree, so returning the
    /// root node keeps every native resource alive for the whole test.
    private func parse(_ source: String,
                       file: StaticString = #filePath,
                       line: UInt = #line) throws -> Node {
        let parser = Parser()
        try parser.setLanguage(Language(language: tree_sitter_skript()))
        let tree = try XCTUnwrap(parser.parse(source), "parser returned no tree", file: file, line: line)
        return try XCTUnwrap(tree.rootNode, "tree has no root node", file: file, line: line)
    }

    /// Byte offsets are measured in the UTF-16LE encoding the parser consumed,
    /// so the ASCII sources below have twice as many bytes as characters.
    private func bytes(_ characterRange: Range<Int>) -> Range<UInt32> {
        UInt32(characterRange.lowerBound * 2)..<UInt32(characterRange.upperBound * 2)
    }

    private func namedChildren(of node: Node,
                               file: StaticString = #filePath,
                               line: UInt = #line) throws -> [Node] {
        try (0..<node.namedChildCount).map { index in
            try XCTUnwrap(node.namedChild(at: index),
                          "no named child at \(index)", file: file, line: line)
        }
    }

    func testNestedBlockCommentSpansWholeCommentAndKeepsFollowingForm() throws {
        let root = try parse(nestedComment)
        XCTAssertFalse(root.hasError)

        let children = try namedChildren(of: root)
        XCTAssertEqual(children.map(\.nodeType), ["block_comment", "num_lit"])

        XCTAssertEqual(children[0].text, nestedCommentText)
        XCTAssertEqual(children[0].byteRange, bytes(0..<28))
        XCTAssertEqual(children[1].text, "42")
        XCTAssertEqual(children[1].byteRange, bytes(29..<31))
    }

    func testUnterminatedBlockCommentOwnsTheRestOfTheInput() throws {
        let root = try parse(unterminatedComment)
        XCTAssertFalse(root.hasError)

        // Exactly two forms: the closed call, then the comment that swallowed
        // the second line. Nothing behind the opener reads as code.
        let children = try namedChildren(of: root)
        XCTAssertEqual(children.map(\.nodeType), ["call", "block_comment"])

        XCTAssertEqual(children[0].text, "(+ 1 2)")
        XCTAssertEqual(children[0].byteRange, bytes(0..<7))
        XCTAssertEqual(children[1].text, unterminatedCommentText)
        XCTAssertEqual(children[1].byteRange, bytes(8..<31))
    }

    func testLegacyDefnParamsCarryTextAndByteRange() throws {
        let root = try parse(legacyDefn)
        XCTAssertFalse(root.hasError)

        let defn = try XCTUnwrap(root.namedChild(at: 0))
        XCTAssertEqual(defn.nodeType, "defn")
        // The legacy shape carries no `name` field; the bound name is the first
        // arglist element, which is what the highlights query keys on.
        XCTAssertNil(defn.child(byFieldName: "name"))

        let params = try XCTUnwrap(defn.child(byFieldName: "params"))
        XCTAssertEqual(params.nodeType, "list")
        XCTAssertEqual(params.text, "(rgba r g)")
        XCTAssertEqual(params.byteRange, bytes(6..<16))

        let names = try namedChildren(of: params)
        XCTAssertEqual(names.map(\.nodeType), ["identifier", "identifier", "identifier"])
        XCTAssertEqual(names[0].text, "rgba")
        XCTAssertEqual(names[0].byteRange, bytes(7..<11))
        XCTAssertEqual(names[1].text, "r")
        XCTAssertEqual(names[1].byteRange, bytes(12..<13))
    }

    func testDefnNameAndParamsFieldsCarryTextAndByteRange() throws {
        let root = try parse(modernDefn)
        XCTAssertFalse(root.hasError)

        let defn = try XCTUnwrap(root.namedChild(at: 0))
        XCTAssertEqual(defn.nodeType, "defn")

        let name = try XCTUnwrap(defn.child(byFieldName: "name"))
        XCTAssertEqual(name.text, "add")
        XCTAssertEqual(name.byteRange, bytes(6..<9))

        let params = try XCTUnwrap(defn.child(byFieldName: "params"))
        XCTAssertEqual(params.text, "(a b)")
        XCTAssertEqual(params.byteRange, bytes(10..<15))
    }

    func testDottedListTailFieldPointsAtTheLastExpression() throws {
        let root = try parse(dottedCall)
        XCTAssertFalse(root.hasError)

        let call = try XCTUnwrap(root.namedChild(at: 0))
        XCTAssertEqual(call.nodeType, "call")
        XCTAssertEqual(call.byteRange, bytes(0..<9))

        let tail = try XCTUnwrap(call.child(byFieldName: "tail"))
        XCTAssertEqual(tail.nodeType, "identifier")
        XCTAssertEqual(tail.text, "c")
        XCTAssertEqual(tail.byteRange, bytes(7..<8))
    }
}
