// swift-tools-version:5.9

import PackageDescription

let package = Package(
    name: "TreeSitterSkript",
    products: [
        .library(name: "TreeSitterSkript", targets: ["TreeSitterSkript"]),
    ],
    dependencies: [
        // 0.10.0 is the first SwiftTreeSitter that vendors tree-sitter core
        // 0.25, which is the runtime that accepts this grammar's ABI 15
        // parser; 0.9.0 vendors 0.23 and SetLanguage rejects it outright.
        .package(url: "https://github.com/tree-sitter/swift-tree-sitter", .upToNextMinor(from: "0.10.0")),
    ],
    targets: [
        .target(
            name: "TreeSitterSkript",
            dependencies: [],
            path: ".",
            sources: ["src/parser.c", "src/scanner.c"],
            resources: [
                .copy("queries")
            ],
            publicHeadersPath: "bindings/swift",
            cSettings: [.headerSearchPath("src")]
        ),
        .testTarget(
            name: "TreeSitterSkriptTests",
            dependencies: [
                .product(name: "SwiftTreeSitter", package: "swift-tree-sitter"),
                "TreeSitterSkript",
            ],
            path: "bindings/swift/TreeSitterSkriptTests"
        )
    ],
    cLanguageStandard: .c11
)