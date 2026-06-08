import XCTest
import SwiftTreeSitter
import TreeSitterSkript

final class TreeSitterSkriptTests: XCTestCase {
    func testCanLoadGrammar() throws {
        let parser = Parser()
        let language = Language(language: tree_sitter_skript())
        XCTAssertNoThrow(try parser.setLanguage(language),
                         "Error loading Skript grammar")
    }
}
