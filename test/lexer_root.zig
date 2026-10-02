//! Scratch root for the Skript lexer oracle driven by `test/verify.py`.
//!
//! `verify.py` copies this file to `<work-dir>/oracle/root.zig`, next to a
//! byte-for-byte copy of `<skript>/src/Token.zig`. The copy must sit beside a
//! file literally named `root.zig` because the live lexer resolves its imports
//! relative to its own directory:
//!
//! ```zig
//! const skript = @import("root.zig"); // ../skript/src/Token.zig:34
//! const Error = skript.Error; // Token.zig:35
//! pub const Index = skript.Index(@This(), u32); // Token.zig:98
//! ```
//!
//! Only those two declarations are reproduced here, plus the oracle entry
//! point. `verify.py` re-hashes the copy against the Skript checkout on every
//! run and aborts if a single byte differs, so no lexer logic can live here:
//! every token, kind, byte range and lexer error is produced by the unmodified
//! Skript source.
//!
//! Writes one JSON object per input file to stdout, in argument order:
//!
//!   {"status":"ok","path":P,"source_len":N,"tokens":[{"kind":..,"start":..,"end":..,"line":..,"col":..}..]}
//!   {"status":"error","path":P,"source_len":N,"error":Name,"offset":B,"line":L,"col":C,"tokens":[..]}
//!   {"status":"io_error","path":P,"error":Name}
//!
//! `status` of `error` means `Token.lex` returned an error; `offset` is the
//! cursor position at the moment of failure, which is the evidence the verifier
//! classifies against. Tokens lexed before the failure are still reported, and
//! `eof` tokens are not.

const std = @import("std");

/// Generic typed index.
///
/// Copied verbatim from `../skript/src/root.zig:23-48` (`pub fn Index`) because
/// `Token.zig:98` instantiates it as `Index(Token, u32)`. The lexer itself only
/// needs the declaration to exist; nothing in `Token.zig` calls into it.
pub fn Index(comptime T: type, comptime I: type) type {
    return struct {
        pub const Target = T;
        pub const IndexType = I;
        const Self = @This();
        index: IndexType,

        pub const @"null": Self = .{ .index = std.math.maxInt(I) };

        pub fn get(self: Self, arr: []const T) ?*const T {
            return if (std.meta.eql(self, Self.null)) null else &arr[self.index];
        }

        pub fn getMut(self: Self, arr: []T) ?*T {
            return if (std.meta.eql(self, Self.null)) null else &arr[self.index];
        }

        pub fn fromInt(int: anytype) Self {
            return .{ .index = @intCast(int) };
        }

        pub fn offset(self: Self, offs: I) Self {
            return .{ .index = self.index + offs };
        }
    };
}

/// The Skript error set.
///
/// Same members as `../skript/src/root.zig:50-81`, minus the `|| Expr.Error`
/// union at line 82. `Expr.Error` comes from `expr.zig` and drags the whole
/// evaluator graph in with it; the lexer provably raises none of it, so the only
/// members that matter are `UnexpectedToken` (from `skipBlockComment`,
/// `lexNumber`, `lexString` and `lexRadixNumber`) and `OutOfMemory`. The rest are
/// kept so this file stays a faithful copy of the upstream declaration instead
/// of a narrowed invention.
pub const Error = error{
    Unimplemented,
    UnexpectedToken,
    OutOfMemory,
    Unbound,
    /// An import or export would claim an existing or reserved name.
    NameConflict,
    UninitializedBinding,
    WriteFailed,
    StackOverflow,
    Assertion,
    TypeMismatch,
    Unsupported,
    Denied,
    Exhausted,
    ArityMismatch,
    /// A differentiation parameter selector does not name a formal argument.
    InvalidSelector,
    MalformedAst,
    InvalidCode,
    StackUnderflow,
    WouldBlock,
    /// Module load cycle: a module (transitively) loads itself (A→B→A).
    CircularLoad,
    /// A user `(raise X)` unwound: the raised payload lives in
    /// Fiber.raised / Process.raised; the error VALUE's anyerror payload
    /// is this tag for host compat.
    Raised,
    /// Terminal exit (05-io-system §4.4; the `exit`/`quit` intrinsics are a
    /// later IO tranche; the enum member exists so try/catch EXCLUDES it:
    /// exit is terminal, never catchable).
    Exit,
};

/// Reported instead of spinning when the lexer stops making progress.
const Stuck = error{Stuck};

/// The unmodified Skript lexer.
pub const Token = @import("Token.zig");
pub fn main(init: std.process.Init) !void {
    const gpa = init.gpa;
    const io = init.io;
    const arena = init.arena.allocator();

    const args = try init.minimal.args.toSlice(arena);
    if (args.len < 2) {
        var usage: std.Io.Writer.Allocating = .init(gpa);
        defer usage.deinit();
        try usage.writer.writeAll(
            "{\"status\":\"usage\",\"expected\":\"<source-file> [<source-file> ...]\",\"given\":0}\n",
        );
        try std.Io.File.stdout().writeStreamingAll(io, usage.written());
        std.process.exit(2);
    }

    var out: std.Io.Writer.Allocating = .init(gpa);
    defer out.deinit();
    const w = &out.writer;

    for (args[1..], 1..) |arg, i| {
        const path: []const u8 = arg;
        if (i != 1) try w.writeByte('\n');
        lexOne(w, io, gpa, path) catch |err| switch (err) {
            error.OutOfMemory => return err,
            else => {
                try w.writeAll("{\"status\":\"io_error\",\"path\":");
                try writeJsonString(w, path);
                try w.print(",\"error\":\"{s}\"}}\n", .{@errorName(err)});
            },
        };
    }

    try std.Io.File.stdout().writeStreamingAll(io, out.written());
}

fn lexOne(w: *std.Io.Writer, io: std.Io, gpa: std.mem.Allocator, path: []const u8) !void {
    const src = std.Io.Dir.cwd().readFileAlloc(io, path, gpa, .unlimited) catch |err| {
        try w.writeAll("{\"status\":\"io_error\",\"path\":");
        try writeJsonString(w, path);
        try w.print(",\"error\":\"{s}\"}}\n", .{@errorName(err)});
        return;
    };
    defer gpa.free(src);

    var toks: std.ArrayList(Token) = .empty;
    defer toks.deinit(gpa);

    var failure: ?struct { name: []const u8, offset: u32, line: u16, col: u16 } = null;
    var cursor: Token.Cursor = .{ .src = src };

    while (true) {
        const tok = Token.lex(&cursor) catch |err| {
            failure = .{
                .name = @errorName(err),
                .offset = @intCast(cursor.offset),
                .line = cursor.line,
                .col = cursor.col,
            };
            break;
        };
        if (tok.kind == .eof) break;
        if (tok.start == tok.end) {
            failure = .{
                .name = @errorName(error.Stuck),
                .offset = tok.start,
                .line = tok.line,
                .col = tok.col,
            };
            break;
        }
        try toks.append(gpa, tok);
    }

    if (failure) |f| {
        try w.writeAll("{\"status\":\"error\",\"path\":");
        try writeJsonString(w, path);
        try w.print(
            ",\"source_len\":{d},\"error\":\"{s}\",\"offset\":{d},\"line\":{d},\"col\":{d},\"tokens\":[",
            .{ src.len, f.name, f.offset, f.line, f.col },
        );
    } else {
        try w.writeAll("{\"status\":\"ok\",\"path\":");
        try writeJsonString(w, path);
        try w.print(",\"source_len\":{d},\"tokens\":[", .{src.len});
    }

    for (toks.items, 0..) |tok, i| {
        if (i != 0) try w.writeByte(',');
        try w.print(
            "{{\"kind\":\"{s}\",\"start\":{d},\"end\":{d},\"line\":{d},\"col\":{d}}}",
            .{ @tagName(tok.kind), tok.start, tok.end, tok.line, tok.col },
        );
    }
    try w.writeAll("]}\n");
}

/// Minimal JSON string writer. `std.json.fmtString` is not part of the 0.16
/// public surface, and the only strings emitted here are file paths and
/// `@tagName` enum tags, so a local escaper is the boring choice.
fn writeJsonString(w: *std.Io.Writer, s: []const u8) !void {
    try w.writeByte('"');
    for (s) |c| switch (c) {
        '"' => try w.writeAll("\\\""),
        '\\' => try w.writeAll("\\\\"),
        '\n' => try w.writeAll("\\n"),
        '\r' => try w.writeAll("\\r"),
        '\t' => try w.writeAll("\\t"),
        0x00...0x07, 0x0b, 0x0c, 0x0e...0x1f => try w.print("\\u{x:0>4}", .{c}),
        else => try w.writeByte(c),
    };
    try w.writeByte('"');
}
