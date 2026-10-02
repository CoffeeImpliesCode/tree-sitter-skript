#!/usr/bin/env python3
"""Behavioural regression checks for the Skript grammar's tree-sitter queries.

The real `tree-sitter` CLI drives everything here; no query source is inspected.

  * ``tree-sitter parse`` - precondition that fixtures parse without error
    recovery, plus the byte spans a ``datum_comment`` owns, read through a tiny
    scratch query of our own.
  * ``tree-sitter query`` - exact capture ranges for outline and brackets.
  * ``tree-sitter highlight --html --css-classes`` - the rendered document, from
    which the *effective* class of every byte is taken: the innermost <span> that
    wins at render time.  A query can capture a node and still lose every byte of
    it to a competing inner capture; only the rendered document shows that.

test/highlight/skript.pt carries the tree-sitter-native form of the highlight
expectations, so ``tree-sitter test`` guards those too.  It asserts the queries'
own capture names; this file asserts the classes the highlighter resolves them to.

    python3 test/queries.py [-v] [--keep] [--workdir PARENT]

Exit codes: 0 all passed, 1 a check failed, 2 the harness could not run.

Scratch: only a PARENT is ever chosen or created (--workdir, else
$TS_QUERIES_TEST_WORKDIR, else $XDG_CACHE_HOME/tree-sitter-skript-tests, else
~/.cache/tree-sitter-skript-tests).  Each run makes one uniquely named child of
it with mkdtemp and deletes that child alone; the parent is never removed, even
on failure.  Parent and child are both resolved through symlinks and refused if
they land under /tmp, /var/tmp, /private/tmp, /dev/shm or /run/shm.
TREE_SITTER_LIBDIR, TMPDIR/TEMP/TMP, the XDG directories and HOME all point
inside the child, so the real parser cache is untouched and the repository is
never used as a build directory.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from html.parser import HTMLParser
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
QUERIES = REPO / "queries"
PARSER_C = REPO / "src" / "parser.c"
LANGUAGE = "skript"
FORBIDDEN_ROOTS = ("/tmp", "/var/tmp", "/private/tmp", "/dev/shm", "/run/shm")
DEFAULT_PARENT = "tree-sitter-skript-tests"

# Classes the highlighter resolves the queries' captures to.
COMMENT = ("comment",)
FUNCTION = ("function",)
BUILTIN = ("function", "builtin")
PARAMETER = ("variable", "parameter")
VARIABLE = ("variable",)
CONSTANT = ("constant",)
QUOTE = ("punctuation", "special")

MIN_DISCARD_NESTING = 8  # delimiter depth of the discarded form below

# An identifier token ends at whitespace and at ()[]{};"'; a bare substring
# search would happily match the `a` inside `legacy-name`.
TOKEN_BODY = r"""[^\s()\[\]{};"']"""
CLOSERS = {")": "(", "]": "[", "}": "{"}
CAPTURE_RE = re.compile(
    r"^\s*capture:\s*\d+\s*-\s*(?P<name>[^\s,]+),\s*"
    r"start:\s*\((?P<sr>\d+),\s*(?P<sc>\d+)\),\s*"
    r"end:\s*\((?P<er>\d+),\s*(?P<ec>\d+)\)"
)
PATTERN_RE = re.compile(r"^\s*pattern:\s*\d+\s*$")


class HarnessError(Exception):
    """The harness could not run; distinct from a failing check."""


class CheckFailure(AssertionError):
    """A behavioural expectation did not hold."""


# --- fixtures -------------------------------------------------------------

OUTLINE_SOURCE = [
    "(def answer 42)",
    "(defn modern (p q) p)",
    "(defn (legacy-name a b) (write a))",
    "(defn standalone 42)",
    "(fn (x) x)",
]
# (line, whole form, expected @name or None, expected @context or None)
OUTLINE_EXPECTED = [
    (0, "(def answer 42)", "answer", None),
    (1, "(defn modern (p q) p)", "modern", "(p q)"),
    (2, "(defn (legacy-name a b) (write a))", "legacy-name", None),
    (3, "(defn standalone 42)", "standalone", None),
    (4, "(fn (x) x)", None, "(x)"),
]

BRACKET_SOURCE = [
    "(set m :{ :a 1 :b :{ :c [2 { 3 + 4 }] } :d (write 5) })",
    "(write (abs -1) [6 7])",
]

# A discarded form: nested definition, nested calls, intrinsic, literal, quoted
# form, punctuation, maps, and a nested datum comment.
DISCARDED_FORM = (
    '(defn (gone a) (write @rgba "s" :k #t 42 [1 2] #;'
    + "(n0 (n1 (n2 (n3 (n4 (n5 @i64.add))))))"
    + " 'quoted (abs -1) :{ :m :{ :n 7 } }))"
)

HIGHLIGHT_SOURCE = [
    "(defn (legacy-name a b) (write a))",  # 0 legacy shape
    "(defn modern (p q) p)",  # 1 modern shape
    "(defn standalone 42)",  # 2 named, no arglist
    "#;" + DISCARDED_FORM,  # 3 discarded, deeply nested
    '(write "kept" 1)',  # 4 live code that follows
    "(write '(a 1) 'b)",  # 5 quoted forms
]
DATUM_LINE, FOLLOWING_LINE, QUOTE_LINE = 3, 4, 5


class Fixture:
    """Source text plus the byte bookkeeping the CLI's offsets need."""

    def __init__(self, name: str, lines: list[str]):
        self.name, self.lines = name, list(lines)
        self.text = "".join(line + "\n" for line in self.lines)
        self.data = self.text.encode()
        if not self.data.isascii():
            raise HarnessError(
                f"fixture {name} is not ASCII; byte offsets would be ambiguous"
            )
        self.line_starts = [0]
        for line in self.lines:
            self.line_starts.append(self.line_starts[-1] + len(line) + 1)
        self.path: Path | None = None

    def line_span(self, index: int) -> tuple[int, int]:
        return self.line_starts[index], self.line_starts[index] + len(self.lines[index])

    def locate(self, line: int, needle: str, occurrence: int = 1, token: bool = False):
        """Byte span of `needle` on `line`, optionally as a whole identifier."""
        text = self.lines[line]
        pattern = re.compile(
            rf"(?<!{TOKEN_BODY}){re.escape(needle)}(?!{TOKEN_BODY})"
            if token
            else re.escape(needle)
        )
        position = -1
        for _ in range(occurrence):
            found = pattern.search(text, position + 1)
            if found is None:
                raise HarnessError(
                    f"fixture {self.name}: {needle!r} (occurrence {occurrence}) is not on "
                    f"line {line + 1}: {text!r}"
                )
            position = found.start()
        base = self.line_starts[line]
        return base + position, base + position + len(needle)

    def show(self, start: int, end: int) -> str:
        """Overlapping source lines with a caret under the offending bytes."""
        out = []
        for number, (text, base) in enumerate(
            zip(self.lines, self.line_starts), start=1
        ):
            stop = base + len(text)
            if stop <= start or base >= end:
                continue
            prefix = f"      line {number}: "
            out.append(prefix + text)
            out.append(
                prefix
                + " " * (max(start, base) - base)
                + "^" * max(1, min(end, stop) - max(start, base))
            )
            if len(out) >= 6:
                break
        out.append(f"      bytes {start}..{end} = {self.data[start:end]!r}")
        return "\n".join(out)


# --- rendered highlight ---------------------------------------------------


class Rendered(HTMLParser):
    """Reads `--html --css-classes` into per-byte effective classes.

    Data arrives in document order with the innermost <span> on top of the stack,
    so the class in force for each byte is what an editor actually paints.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.in_line = self.offset = 0
        self.stack: list[tuple[int, tuple[str, ...]]] = []
        self.chunks: list[tuple[str, tuple[str, ...]]] = []
        self.spans: list[tuple[int, int, tuple[str, ...]]] = []

    def handle_starttag(self, tag, attrs):
        attributes = {key: (value or "") for key, value in attrs}
        if tag == "td":
            self.in_line = "line" in attributes.get("class", "").split()
        elif tag == "span" and self.in_line:
            self.stack.append((self.offset, tuple(attributes.get("class", "").split())))

    def handle_endtag(self, tag):
        if tag == "span" and self.stack:
            start, classes = self.stack.pop()
            if self.offset > start:
                self.spans.append((start, self.offset, classes))
        elif tag == "td":
            self.in_line = False

    def handle_data(self, data):
        if self.in_line:
            self.chunks.append((data, self.stack[-1][1] if self.stack else ()))
            self.offset += len(data)


class Highlight:
    """Effective styling of one rendered fixture."""

    def __init__(self, fixture: Fixture, document: str):
        parser = Rendered()
        parser.feed(document)
        parser.close()
        text = "".join(chunk for chunk, _ in parser.chunks)
        if text != fixture.text:
            raise HarnessError(
                "the rendered highlight document does not reproduce the fixture byte for "
                f"byte, so offsets cannot be trusted\n      expected {fixture.text!r}\n"
                f"      got      {text!r}"
            )
        self.fixture, self.spans = fixture, parser.spans
        self.effective: list[tuple[str, ...]] = []
        for chunk, classes in parser.chunks:
            self.effective.extend([classes] * len(chunk))

    def at(self, offset: int) -> tuple[str, ...]:
        return self.effective[offset]

    def covered_by(self, start: int, end: int, classes: tuple[str, ...]) -> bool:
        return any(
            span_classes == classes and span_start <= start and span_end >= end
            for span_start, span_end, span_classes in self.spans
        )


# --- CLI ------------------------------------------------------------------


class Cli:
    def __init__(self, binary: str, scratch: Path, verbose: bool = False):
        self.binary, self.scratch, self.verbose = binary, scratch, verbose
        self.lib = scratch / "libdir" / f"{LANGUAGE}.so"
        self.env = dict(os.environ)
        self.env.update(
            TREE_SITTER_LIBDIR=str(scratch / "libdir"),
            TMPDIR=str(scratch / "tmp"),
            TEMP=str(scratch / "tmp"),
            TMP=str(scratch / "tmp"),
            XDG_CACHE_HOME=str(scratch / "xdg-cache"),
            XDG_CONFIG_HOME=str(scratch / "xdg-config"),
            XDG_DATA_HOME=str(scratch / "xdg-data"),
            HOME=str(scratch / "home"),
        )
        for name in (
            "libdir",
            "tmp",
            "src",
            "home",
            "xdg-cache",
            "xdg-config",
            "xdg-data",
        ):
            (scratch / name).mkdir(parents=True, exist_ok=True)

    def run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        command = [self.binary, *args]
        if self.verbose:
            print("    $ " + " ".join(command), file=sys.stderr)
        proc = subprocess.run(
            command,
            cwd=self.scratch,
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )
        if check and proc.returncode != 0:
            raise HarnessError(
                f"`{' '.join(command)}` exited {proc.returncode}\n{proc.stdout}{proc.stderr}"
            )
        return proc

    def build(self) -> None:
        if not (
            self.lib.is_file() and self.lib.stat().st_mtime >= PARSER_C.stat().st_mtime
        ):
            self.run("build", "-o", str(self.lib), str(REPO))

    def materialise(self, fixture: Fixture) -> None:
        fixture.path = self.scratch / "src" / f"{fixture.name}.pt"
        fixture.path.write_text(fixture.text)

    def parse_is_clean(self, fixture: Fixture) -> bool:
        proc = self.run(
            "parse",
            "--lib-path",
            str(self.lib),
            "--lang-name",
            LANGUAGE,
            str(fixture.path),
            check=False,
        )
        return "ERROR" not in proc.stdout and "MISSING" not in proc.stdout

    def query(
        self, query_path: Path, fixture: Fixture
    ) -> list[list[tuple[str, int, int]]]:
        """Every match, as a list of (capture name, start byte, end byte)."""
        proc = self.run(
            "query",
            "--lib-path",
            str(self.lib),
            "--lang-name",
            LANGUAGE,
            str(query_path),
            str(fixture.path),
            check=False,
        )
        if proc.returncode != 0:
            raise HarnessError(
                f"query {query_path.name} failed:\n{proc.stdout}{proc.stderr}"
            )
        starts = [0]
        for index, byte in enumerate(fixture.data):
            if byte == 0x0A:
                starts.append(index + 1)
        matches: list[list[tuple[str, int, int]]] = []
        for line in proc.stdout.splitlines():
            if PATTERN_RE.match(line):
                matches.append([])
                continue
            capture = CAPTURE_RE.match(line)
            if capture and matches:
                start_row, end_row = int(capture.group("sr")), int(capture.group("er"))
                if end_row >= len(starts):
                    raise HarnessError(
                        f"query reported row {end_row} past the end of the fixture"
                    )
                matches[-1].append(
                    (
                        capture.group("name"),
                        starts[start_row] + int(capture.group("sc")),
                        starts[end_row] + int(capture.group("ec")),
                    )
                )
        return matches

    def highlight(self, fixture: Fixture) -> Highlight:
        proc = self.run(
            "highlight",
            "--html",
            "--css-classes",
            "--grammar-path",
            str(REPO),
            str(fixture.path),
            check=False,
        )
        if proc.returncode != 0:
            raise HarnessError(f"highlight failed:\n{proc.stdout}{proc.stderr}")
        return Highlight(fixture, proc.stdout)


# --- scratch handling -----------------------------------------------------


def _refuse_forbidden(path: Path) -> Path:
    """Resolve through symlinks, then refuse anything under a volatile root."""
    resolved = Path(os.path.realpath(path))
    for forbidden in FORBIDDEN_ROOTS:
        root = Path(forbidden)
        if resolved == root or root in resolved.parents:
            raise HarnessError(
                f"refusing to use {resolved}: it lives under {forbidden}. Pass --workdir or "
                "set TS_QUERIES_TEST_WORKDIR to a persistent path."
            )
    return resolved


def resolve_parent(explicit: str | None) -> Path:
    """Choose a PARENT directory only.  Nothing chosen here is ever removed."""
    if explicit:
        parent = Path(explicit)
    elif os.environ.get("TS_QUERIES_TEST_WORKDIR"):
        parent = Path(os.environ["TS_QUERIES_TEST_WORKDIR"])
    else:
        base = os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")
        parent = Path(base) / DEFAULT_PARENT
    return _refuse_forbidden(parent.expanduser())


@contextmanager
def scratch_run(explicit: str | None, keep: bool):
    """A uniquely named child of the chosen parent; only that child is removed."""
    parent = resolve_parent(explicit)
    parent.mkdir(parents=True, exist_ok=True)
    _refuse_forbidden(parent)
    child = _refuse_forbidden(Path(tempfile.mkdtemp(prefix="run-", dir=parent)))
    try:
        yield child
    finally:
        if keep:
            print(f"scratch kept at {child}")
        else:
            shutil.rmtree(child, ignore_errors=True)


# --- assertions -----------------------------------------------------------


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise CheckFailure(message)


def expect_classes(highlight: Highlight, start: int, end: int, want, what: str) -> None:
    """Every byte in [start, end) must render with exactly `want`."""
    fixture = highlight.fixture
    seen: dict[tuple, list[int]] = {}
    for offset in range(start, end):
        classes = highlight.at(offset)
        if classes != want:
            seen.setdefault(classes, []).append(offset)
    if not seen:
        return
    detail = "\n".join(
        f"        {len(offsets):>4} byte(s) render as {list(classes)}, e.g. "
        f"{fixture.data[offsets[0]]!r} at byte {offsets[0]}"
        for classes, offsets in sorted(seen.items(), key=lambda item: min(item[1]))
    )
    first = min(min(offsets) for offsets in seen.values())
    total = sum(len(offsets) for offsets in seen.values())
    raise CheckFailure(
        f"{what}: all {end - start} byte(s) of {fixture.data[start:end]!r} must render as "
        f"{list(want)}, but {total} do not\n{fixture.show(first, first + 1)}\n"
        f"      observed:\n{detail}"
    )


def expect_no_classes(
    highlight: Highlight, start: int, end: int, forbidden, what: str
) -> None:
    offenders = [o for o in range(start, end) if highlight.at(o) == forbidden]
    if offenders:
        raise CheckFailure(
            f"{what}: {len(offenders)} of {end - start} byte(s) must not render as "
            f"{list(forbidden)}\n{highlight.fixture.show(offenders[0], offenders[0] + 1)}"
        )


# --- independent reference: a delimiter stack over the fixture bytes -----


def bracket_pairs(text: str, offset: int = 0) -> list[tuple[int, int, int, int]]:
    """Expected (open_start, open_end, close_start, close_end) spans.

    `:{` is one opener token in this grammar, so its span starts at the colon
    while the delimiter it introduces is still `{`.
    """
    pairs, stack, index = [], [], 0
    while index < len(text):
        char = text[index]
        if char == ":" and text[index + 1 : index + 2] == "{":
            stack.append(("{", index, 2))
            index += 2
            continue
        if char in "([{":
            stack.append((char, index, 1))
        elif char in CLOSERS:
            if not stack or stack[-1][0] != CLOSERS[char]:
                raise HarnessError(
                    f"fixture delimiters are unbalanced at byte {offset + index}"
                )
            _, start, width = stack.pop()
            pairs.append(
                (
                    offset + start,
                    offset + start + width,
                    offset + index,
                    offset + index + 1,
                )
            )
        index += 1
    if stack:
        raise HarnessError("fixture leaves a delimiter unclosed")
    return pairs


def max_nesting(text: str) -> int:
    depth = deepest = index = 0
    while index < len(text):
        char = text[index]
        if char == ":" and text[index + 1 : index + 2] == "{":
            depth, index = depth + 1, index + 1
        elif char in "([{":
            depth += 1
        elif char in CLOSERS:
            depth -= 1
        else:
            index += 1
            continue
        deepest = max(deepest, depth)
        index += 1
    return deepest


# --- checks ---------------------------------------------------------------


def check_fixture_parses(cli: Cli, hl: Highlight) -> None:
    """The highlight fixtures must parse without error recovery."""
    expect(
        cli.parse_is_clean(hl.fixture),
        f"fixture {hl.fixture.name} parses with ERROR/MISSING nodes, so the highlight "
        "results would be measured against error recovery",
    )


def check_outline(cli: Cli, hl: Highlight) -> None:
    """Legacy, modern and params-free definitions are outline-visible."""
    fixture = Fixture("outline", OUTLINE_SOURCE)
    cli.materialise(fixture)
    expect(
        cli.parse_is_clean(fixture), f"fixture {fixture.name} does not parse cleanly"
    )
    matches = cli.query(QUERIES / "outline.scm", fixture)

    def named(match, name):
        return [(s, e) for capture, s, e in match if capture == name]

    def describe(match):
        return "        " + (
            " ".join(f"@{c}={fixture.data[s:e]!r}" for c, s, e in match) or "-"
        )

    listing = (
        "\n".join(map(describe, matches)) or "        (the query captured nothing)"
    )

    for line, form, want_name, want_context in OUTLINE_EXPECTED:
        start, end = fixture.locate(line, form)
        owners = [
            m
            for m in matches
            if any(s == start and e == end for s, e in named(m, "item"))
        ]
        expect(
            len(owners) == 1,
            f"{form}: expected exactly one @item spanning bytes {start}..{end}, found "
            f"{len(owners)}\n      captures:\n{listing}",
        )
        owner = owners[0]

        names = named(owner, "name")
        if want_name is None:
            expect(
                not names, f"{form}: anonymous, so no @name\n      captures:\n{listing}"
            )
        else:
            expect(
                len(names) == 1,
                f"{form}: expected one @name, found {len(names)}\n      captures:\n{listing}",
            )
            expect(
                names[0] == fixture.locate(line, want_name, token=True),
                f"{form}: @name is bytes {names[0][0]}..{names[0][1]} = "
                f"{fixture.data[names[0][0] : names[0][1]]!r}, expected {want_name!r}\n"
                f"      captures:\n{listing}",
            )

        contexts = named(owner, "context")
        if want_context is None:
            expect(
                not contexts,
                f"{form}: no argument list exists, so @context must be absent\n"
                f"      captures:\n{listing}",
            )
        else:
            want = fixture.locate(line, want_context)
            expect(
                contexts == [want],
                f"{form}: @context is {contexts}, expected the argument list {want_context!r} "
                f"at bytes {want}\n      captures:\n{listing}",
            )

    seen = sorted(
        fixture.data[s:e].decode() for m in matches for s, e in named(m, "name")
    )
    wanted = sorted(name for _, _, name, _ in OUTLINE_EXPECTED if name)
    expect(
        seen == wanted,
        f"every @name captured is {seen}, expected {wanted}: an argument of a defn or fn "
        f"arglist must never be captured as a name\n      captures:\n{listing}",
    )


def check_brackets(cli: Cli, hl: Highlight) -> None:
    """Every delimiter pairs with its own closer - maps included."""
    fixture = Fixture("brackets", BRACKET_SOURCE)
    cli.materialise(fixture)
    expect(
        cli.parse_is_clean(fixture), f"fixture {fixture.name} does not parse cleanly"
    )
    matches = cli.query(QUERIES / "brackets.scm", fixture)

    observed, malformed = [], []
    for match in matches:
        opens = [(s, e) for capture, s, e in match if capture == "open"]
        closes = [(s, e) for capture, s, e in match if capture == "close"]
        if len(opens) != 1 or len(closes) != 1:
            malformed.append(
                "        "
                + " ".join(f"@{c}={fixture.data[s:e]!r}" for c, s, e in match)
            )
            continue
        observed.append((opens[0][0], opens[0][1], closes[0][0], closes[0][1]))

    if malformed:
        raise CheckFailure(
            "every brackets match must pair one @open with one @close; these do not:\n"
            + "\n".join(malformed)
        )

    expected = bracket_pairs(fixture.text)
    problems = [
        f"        {fixture.data[a:b]!r} at bytes {a}..{b} has no capture pairing it with the "
        f"{fixture.data[c:d]!r} at bytes {c}..{d}\n{fixture.show(a, b)}"
        for a, b, c, d in expected
        if (a, b, c, d) not in observed
    ]
    problems += [
        f"        {fixture.data[a:b]!r} {a}..{b} -> {fixture.data[c:d]!r} {c}..{d} is not a "
        "matching pair"
        for a, b, c, d in observed
        if (a, b, c, d) not in expected
    ]
    problems += [
        f"        {fixture.data[a:b]!r} at {a}..{b} is captured more than once"
        for a, b, _, _ in sorted({p for p in observed if observed.count(p) > 1})
    ]
    if problems:
        raise CheckFailure("bracket pairing is wrong.\n" + "\n".join(problems))

    for index, (open_start, _, close_start, _) in enumerate(expected):
        for inner_open, _, inner_close, _ in expected[index + 1 :]:
            if open_start < inner_open < close_start < inner_close:
                raise CheckFailure(
                    f"bracket pairs cross: {fixture.data[open_start : open_start + 2]!r} closes at "
                    f"{close_start} but {fixture.data[inner_open : inner_open + 2]!r} closes at "
                    f"{inner_close}\n{fixture.show(open_start, inner_close + 1)}"
                )


def check_highlight_precedence(cli: Cli, hl: Highlight) -> None:
    """A legacy defn name outranks its parameters; modern shapes agree."""
    cases = [
        (0, "legacy-name", 1, FUNCTION, "legacy defn name"),
        (0, "a", 1, PARAMETER, "legacy defn first parameter"),
        (0, "b", 1, PARAMETER, "legacy defn second parameter"),
        (0, "a", 2, VARIABLE, "the parameter used as an ordinary variable"),
        (1, "modern", 1, FUNCTION, "modern defn name"),
        (1, "p", 1, PARAMETER, "modern defn first parameter"),
        (1, "q", 1, PARAMETER, "modern defn second parameter"),
        (1, "p", 2, VARIABLE, "the parameter used as an ordinary variable"),
        (2, "standalone", 1, FUNCTION, "defn without an argument list"),
    ]
    for line, needle, occurrence, want, what in cases:
        start, end = hl.fixture.locate(line, needle, occurrence, token=True)
        expect_classes(hl, start, end, want, what)


def check_datum_comment(cli: Cli, hl: Highlight) -> None:
    """Every byte of a discarded form is comment-styled, at any nesting depth."""
    fixture = hl.fixture
    line_start, line_end = fixture.line_span(DATUM_LINE)
    following_start, following_end = fixture.line_span(FOLLOWING_LINE)

    # The span comes from the grammar's own datum_comment node via a scratch
    # query of ours, never from the highlights query under test.
    scratch_query = cli.scratch / "datum.scm"
    scratch_query.write_text("(datum_comment) @discarded\n")
    ranges = sorted(
        (s, e)
        for match in cli.query(scratch_query, fixture)
        for name, s, e in match
        if name == "discarded"
    )
    expect(
        ranges == [(line_start, line_end)],
        f"datum_comment ranges {ranges} do not cover the complete discarded form "
        f"{line_start}..{line_end}:\n      {fixture.lines[DATUM_LINE]}",
    )

    # Nesting is measured from the bytes: an opaque datum_comment has no children
    # by construction, so a tree-derived depth would measure the fix, not the fixture.
    discarded = fixture.text[line_start:line_end]
    depth = max_nesting(discarded)
    expect(
        depth >= MIN_DISCARD_NESTING,
        f"the discarded form must nest {MIN_DISCARD_NESTING} delimiters deep to exercise "
        f"arbitrary nesting, but the deepest chain is {depth}:\n      {discarded}",
    )
    expect(
        discarded.count("#;") >= 2,
        f"the discarded form must itself contain a nested datum comment:\n      {discarded}",
    )

    for start, end in ranges:
        expect(
            line_start <= start and end <= line_end,
            f"a datum_comment spans bytes {start}..{end}, outside its own line "
            f"{line_start}..{line_end}; a discarded form must not swallow the next live "
            f"form\n{fixture.show(start, min(end, start + 1))}",
        )
        expect_classes(hl, start, end, COMMENT, "a discarded form")

    expect_no_classes(
        hl,
        following_start,
        following_end,
        COMMENT,
        "the live code after a discarded form",
    )
    start, end = fixture.locate(FOLLOWING_LINE, "write", token=True)
    expect_classes(hl, start, end, BUILTIN, "the call after a discarded form")
    start, end = fixture.locate(FOLLOWING_LINE, '"kept"')
    expect_classes(hl, start, end, ("string",), "the string after a discarded form")


def check_quote_retained(cli: Cli, hl: Highlight) -> None:
    """`' FORM is data: the quoted form stays a constant."""
    fixture = hl.fixture
    call_start, call_end = fixture.locate(QUOTE_LINE, "(a 1)")
    bare_start, bare_end = fixture.locate(QUOTE_LINE, "b", token=True)
    quote_start, _ = fixture.locate(QUOTE_LINE, "'(a 1)")

    expect(
        hl.covered_by(call_start, call_end, CONSTANT),
        f"no rendered constant span covers the quoted form at bytes {call_start}..{call_end}\n"
        f"{fixture.show(call_start, call_end)}",
    )
    expect_classes(hl, bare_start, bare_end, CONSTANT, "the identifier inside '")
    expect_classes(hl, quote_start, quote_start + 1, QUOTE, "the quote reader macro")


CHECKS = [
    (
        "fixture: the highlight source parses without error recovery",
        check_fixture_parses,
    ),
    ("outline: legacy, modern and params-free defs are outline-visible", check_outline),
    (
        "brackets: every delimiter pairs with its own closer, maps included",
        check_brackets,
    ),
    (
        "highlight: a legacy defn name outranks its parameters",
        check_highlight_precedence,
    ),
    (
        "datum comments: a discarded form is opaque at any nesting depth",
        check_datum_comment,
    ),
    ("quotes: `' FORM stays a constant", check_quote_retained),
]


def resolve_binary() -> str:
    override = os.environ.get("TREE_SITTER_BIN")
    if override:
        if shutil.which(override) or Path(override).is_file():
            return override
        raise HarnessError(f"TREE_SITTER_BIN={override!r} is not an executable")
    found = shutil.which("tree-sitter")
    if not found:
        raise HarnessError(
            "the `tree-sitter` CLI is required (0.25+); set TREE_SITTER_BIN"
        )
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Behavioural checks for the Skript queries."
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="echo the CLI commands"
    )
    parser.add_argument(
        "--keep", action="store_true", help="keep this run's scratch directory"
    )
    parser.add_argument("--workdir", help="scratch PARENT (never itself removed)")
    args = parser.parse_args(argv)

    try:
        binary = resolve_binary()
        with scratch_run(args.workdir, args.keep) as scratch:
            cli = Cli(binary, scratch, verbose=args.verbose)
            cli.build()
            source = Fixture("highlight", HIGHLIGHT_SOURCE)
            cli.materialise(source)
            highlight = cli.highlight(source)

            failures = []
            for title, check in CHECKS:
                try:
                    check(cli, highlight)
                except (CheckFailure, HarnessError) as exc:
                    failures.append((title, str(exc)))
                    print(
                        f"{'ERROR' if isinstance(exc, HarnessError) else 'FAIL'}  {title}"
                    )
                else:
                    print(f"ok    {title}")
            for title, message in failures:
                print(f"\n--- {title}\n{message}")
            print(f"\n{len(CHECKS) - len(failures)}/{len(CHECKS)} checks passed")
            return 1 if failures else 0
    except HarnessError as exc:
        print(f"queries.py: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
