#!/usr/bin/env python3
"""Self-tests for test/verify.py.

Every test runs the REAL lexer oracle (a byte-verified copy of
`../skript/src/Token.zig`) and the REAL parser probe (the checked-in
`src/parser.c` plus the required `src/scanner.c`) over real Skript source.
There are no mocked oracles and no assertions about the harness's own text: a
test either shows a real divergence landing in the right family, or shows a
real divergence being correctly refused.

    python3 test/test_verify.py            # build both oracles once, then run
    python3 test/test_verify.py -v         # verbose

Needs Zig 0.16, a C compiler and a tree-sitter runtime. If any is missing the
suite skips rather than pretending to have verified something.
"""

from __future__ import annotations

import contextlib
import io
import itertools
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Set before the import so nothing writes a __pycache__ beside the sources:
# the only artifact this suite leaves behind is the run directory it owns.
sys.dont_write_bytecode = True

import verify  # noqa: E402

REPO_ROOT = verify.REPO_ROOT
DEFAULT_SKRIPT = REPO_ROOT.parent / "skript"
TOLERATED = set(verify.CLASSIFIED_FAMILIES) | set(verify.INFORMATIONAL_FAMILIES)
_counter = itertools.count()

_MODULE_WORK: Path | None = None


def unclassified(families) -> list:
    """The families a run would refuse: neither classified nor informational."""
    return [family for family in families if family not in TOLERATED]


def make_view(entries=(), comments=(), missing=(), errors=(), unknown=()):
    """A TreeView built by hand, for the cases no real source can produce."""
    return verify.TreeView(
        entries=list(entries),
        comments=list(comments),
        missing=list(missing),
        errors=list(errors),
        unknown=list(unknown),
    )


def failure(tokens, offset: int, error: str = verify.READER_UNEXPECTED_TOKEN) -> dict:
    """The record the oracle writes when the lexer dies after `tokens`.

    `tokens` are (kind, start, end) triples as the reader saw them, so a
    keyword's start is one byte past the `:` it ate (Token.zig:237-239).
    """
    return {
        "status": "error",
        "error": error,
        "offset": offset,
        "line": 1,
        "col": offset + 1,
        "tokens": [
            {"kind": kind, "start": start, "end": end, "line": 1, "col": start + 1}
            for kind, start, end in tokens
        ],
    }


def setUpModule() -> None:
    """Take one exclusive child of the shared parent for the whole module."""
    global _MODULE_WORK
    _MODULE_WORK = verify.create_run_dir(
        verify.resolve_work_dir(os.environ.get("TREE_SITTER_SKRIPT_TEST_WORKDIR"))
    )


def tearDownModule() -> None:
    global _MODULE_WORK
    if _MODULE_WORK is not None:
        verify.remove_run_dir(_MODULE_WORK)
        _MODULE_WORK = None


class VerifierTestBase(unittest.TestCase):
    tools: verify.Tools

    @classmethod
    def setUpClass(cls) -> None:
        work = _MODULE_WORK
        if work is None:
            raise unittest.SkipTest("no run directory: setUpModule did not complete")
        try:
            runtime = verify.discover_runtime(None, None)
            tools = verify.build_tools(work, DEFAULT_SKRIPT, runtime, verbose=False)
        except verify.HarnessError as exc:
            raise unittest.SkipTest(f"toolchain unavailable: {exc}") from exc
        cls.tools = tools
        (tools.work / "selftest").mkdir(parents=True, exist_ok=True)

    def write(self, label: str, data: bytes) -> Path:
        path = self.tools.work / "selftest" / f"{label}-{next(_counter):05d}.pt"
        path.write_bytes(data)
        return path

    def probe(self, data: bytes):
        nodes, opaque = verify.run_probe_tree(self.tools, [self.write("p", data)])
        return nodes[0], opaque.get(0, [])

    def run_case(self, source: str):
        """Lex + parse one real source; return (oracle, view, divergences, families)."""
        data = source.encode("utf-8")
        path = self.write("case", data)
        oracle = verify.run_oracle(self.tools, [path])[0]
        nodes, opaque = verify.run_probe_tree(self.tools, [path])
        view = verify.tree_view(verify.build_tree(nodes[0]))
        spans = [(o["s"], o["e"]) for o in opaque.get(0, [])]
        divergences = verify.analyse_case(data, oracle, view, spans)
        return oracle, view, divergences, [d.family for d in divergences], spans

    def assertTolerated(self, families, source):
        fatal = unclassified(families)
        self.assertEqual([], fatal, f"unclassified divergence for {source!r}: {fatal}")

    def assertFatal(self, families, source):
        fatal = unclassified(families)
        self.assertTrue(
            fatal, f"{source!r} produced no unclassified divergence: {families}"
        )

    def families_for(self, source: str, view, spans) -> list:
        """Re-run analyse_case over a real oracle record and a HAND-BUILT view.

        This is how a corrupted tree is tested: the oracle half is the real
        lexer, and only the tree half is doctored.
        """
        data = source.encode("utf-8")
        oracle = verify.run_oracle(self.tools, [self.write("doctored", data)])[0]
        divergences = verify.analyse_case(data, oracle, view, spans)
        return [d.family for d in divergences]


class WorkDirTests(unittest.TestCase):
    """The scratch directory must never be /tmp or /var/tmp, however spelled."""

    def test_rejects_tmp_and_var_tmp(self):
        for bad in ("/tmp", "/tmp/verify", "/var/tmp", "/var/tmp/verify/deep"):
            with self.assertRaises(verify.WorkDirError):
                verify.resolve_work_dir(bad)

    def test_rejects_relative_paths_that_land_in_tmp(self):
        cwd = Path.cwd()
        os.chdir("/tmp")
        try:
            with self.assertRaises(verify.WorkDirError):
                verify.resolve_work_dir("verify-scratch")
        finally:
            os.chdir(cwd)

    def test_defaults_to_the_xdg_cache(self):
        saved = os.environ.pop("TREE_SITTER_SKRIPT_VERIFY_WORKDIR", None)
        cache = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
        try:
            chosen = verify.resolve_work_dir(None)
        finally:
            if saved is not None:
                os.environ["TREE_SITTER_SKRIPT_VERIFY_WORKDIR"] = saved
        expected = Path(os.path.realpath(Path(cache) / "tree-sitter-skript" / "verify"))
        self.assertEqual(expected, chosen)

    def test_env_var_is_used_when_no_flag(self):
        previous = os.environ.get("TREE_SITTER_SKRIPT_VERIFY_WORKDIR")
        os.environ["TREE_SITTER_SKRIPT_VERIFY_WORKDIR"] = "/var/tmp/nope"
        try:
            with self.assertRaises(verify.WorkDirError):
                verify.resolve_work_dir(None)
        finally:
            if previous is None:
                del os.environ["TREE_SITTER_SKRIPT_VERIFY_WORKDIR"]
            else:
                os.environ["TREE_SITTER_SKRIPT_VERIFY_WORKDIR"] = previous

    def test_runtime_override_is_validated(self):
        with self.assertRaises(verify.HarnessError):
            verify.discover_runtime("/definitely/not/here", None)
        with self.assertRaises(verify.HarnessError):
            verify.discover_runtime(None, "/definitely/not/here.so")


class WorkOwnershipTests(unittest.TestCase):
    """Whatever already lives in --work-dir must outlive a run, pass or fail."""

    def setUp(self):
        parent = verify.resolve_work_dir(
            os.environ.get("TREE_SITTER_SKRIPT_TEST_WORKDIR")
        )
        parent.mkdir(parents=True, exist_ok=True)
        # Stand-in for a caller's own directory: files and a subdirectory that
        # this harness did not create and must not disturb.
        self.owner = Path(tempfile.mkdtemp(prefix="owner-", dir=parent))
        self.addCleanup(shutil.rmtree, self.owner, ignore_errors=True)
        self.marker = self.owner / "caller.txt"
        self.marker.write_text("caller data", encoding="utf-8")
        self.directory = self.owner / "caller-dir"
        self.directory.mkdir()
        self.nested = self.directory / "nested.txt"
        self.nested.write_text("nested caller data", encoding="utf-8")

    def assert_markers_intact(self):
        self.assertTrue(self.marker.is_file())
        self.assertEqual("caller data", self.marker.read_text(encoding="utf-8"))
        self.assertEqual("nested caller data", self.nested.read_text(encoding="utf-8"))

    def test_run_gets_a_fresh_child_and_caller_files_survive(self):
        with verify.owned_run_dir(self.owner) as run:
            self.assertNotEqual(self.owner, run)
            self.assertEqual(self.owner, run.parent)
            (run / "bin").mkdir()
            self.assert_markers_intact()
        self.assertFalse(run.exists(), "the run directory must go on success")
        self.assert_markers_intact()

    def test_caller_files_survive_a_failed_run(self):
        failed = None
        with self.assertRaises(RuntimeError):
            with verify.owned_run_dir(self.owner) as run:
                failed = run
                raise RuntimeError("the run blew up")
        if failed is None:
            self.fail("the failing run never created its private directory")
        self.assertFalse(failed.exists(), "a failed run must clean up after itself")
        self.assert_markers_intact()

    def test_two_runs_never_share_a_directory(self):
        first = verify.create_run_dir(self.owner)
        second = verify.create_run_dir(self.owner)
        self.addCleanup(verify.remove_run_dir, first)
        self.addCleanup(verify.remove_run_dir, second)
        self.assertNotEqual(first, second)
        self.assertEqual({self.owner}, {first.parent, second.parent})

    def test_keep_reports_the_exact_directory_it_left_behind(self):
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            with verify.owned_run_dir(self.owner, keep=True) as run:
                self.assertTrue(run.is_dir())
        self.assertIn(str(run), printed.getvalue())
        self.assertTrue(run.is_dir())
        verify.remove_run_dir(run)
        self.assert_markers_intact()

    def test_removal_refuses_directories_this_run_did_not_create(self):
        for foreign in (self.owner, self.directory):
            with self.subTest(path=str(foreign)):
                with self.assertRaises(verify.WorkDirError):
                    verify.remove_run_dir(foreign)
        self.assert_markers_intact()

    def test_removal_refuses_a_symlink_named_like_a_run(self):
        target = self.owner / "real-target"
        target.mkdir()
        decoy = self.owner / (verify.RUN_DIR_PREFIX + "decoy")
        decoy.symlink_to(target)
        with self.assertRaises(verify.WorkDirError):
            verify.remove_run_dir(decoy)
        self.assertTrue(
            target.is_dir(), "a refused removal must not touch the link target"
        )

    def test_forbidden_roots_are_refused_for_run_directories(self):
        for bad in ("/tmp", "/var/tmp"):
            with self.subTest(root=bad):
                with self.assertRaises(verify.WorkDirError):
                    verify.create_run_dir(Path(bad))

    def test_a_symlinked_parent_is_resolved_before_anything_is_written(self):
        target = self.owner / "real-target"
        target.mkdir()
        link = self.owner / "link"
        link.symlink_to(target)
        with verify.owned_run_dir(link) as run:
            self.assertEqual(Path(os.path.realpath(target)), run.parent)
            self.assertFalse(run.is_symlink())
        self.assertFalse(run.exists())


class LexerNormalizationTests(VerifierTestBase):
    def test_keyword_colon_is_reattached(self):
        data = b"(:foo 1)"
        oracle = verify.run_oracle(self.tools, [self.write("kw", data)])[0]
        self.assertEqual("ok", oracle["status"])
        entries = verify.reader_view(oracle["tokens"], data)
        self.assertEqual([1, 5], [entries[1][1], entries[1][2]])
        self.assertEqual(b":foo", data[entries[1][1] : entries[1][2]])

    def test_keyword_without_colon_is_a_harness_bug(self):
        # A reader keyword token not preceded by ':' means the normalization
        # rule stopped matching Token.zig. That must be loud, not a divergence.
        with self.assertRaises(verify.HarnessError):
            verify.reader_view(
                [{"kind": "keyword", "start": 3, "end": 6, "line": 1, "col": 4}], b"abc"
            )

    def test_bool_nil_and_head_symbols_fold_to_identifier(self):
        data = b"#t nil #f #true nil? (def x 1) (defn f (a) a) (fn (a) a)"
        records, _ = self.probe(data)
        raw_types = {rec["type"] for rec in records}
        self.assertTrue(
            {"bool_lit", "nil_lit", "def", "defn", "fn"} <= raw_types,
            f"grammar node types changed: {sorted(raw_types)}",
        )
        view = verify.tree_view(verify.build_tree(records))
        kinds = {kind for kind, _, _ in view.entries}
        self.assertNotIn("bool_lit", kinds)
        self.assertNotIn("nil_lit", kinds)
        self.assertIn("identifier", kinds)

    def test_comments_are_excluded_but_recorded(self):
        data = b"; c\n#| b |#\n42"
        records, _ = self.probe(data)
        view = verify.tree_view(verify.build_tree(records))
        self.assertEqual([("number", 12, 14)], view.entries)
        self.assertEqual(
            {("line_comment", 0, 3), ("block_comment", 4, 11)}, set(view.comments)
        )

    def test_missing_nodes_are_dropped_and_counted(self):
        # `(a` is closed by a zero-width MISSING node in the grammar. The reader
        # never produces one, and it claims no bytes, so dropping it is safe.
        data = b"(a"
        records, _ = self.probe(data)
        view = verify.tree_view(verify.build_tree(records))
        self.assertTrue(view.missing, "expected the grammar to insert a MISSING node")
        self.assertEqual([("lparen", 0, 1), ("identifier", 1, 2)], view.entries)

    def test_unknown_leaf_type_is_reported_not_skipped(self):
        node = verify.Node(
            type="brand_new_token",
            field=None,
            start=0,
            end=1,
            start_point=(0, 0),
            end_point=(0, 1),
            named=True,
            missing=False,
            error=False,
            depth=0,
            children=[],
        )
        view = verify.tree_view(node)
        self.assertEqual([("brand_new_token", 0, 1)], view.unknown)
        self.assertEqual(
            [], view.entries, "an unmapped leaf must not reach the comparison"
        )


class ClassificationTests(VerifierTestBase):
    """Every real divergence must land in its family, or fail loudly."""

    def test_number_terminator_leniency(self):
        for source in ("42abc", "1_000", '1"abc"'):
            _, _, _, families, _ = self.run_case(source)
            self.assertIn(verify.FAM_NUM, families, source)
            self.assertTolerated(families, source)

    def test_misplaced_shebang(self):
        _, _, _, families, _ = self.run_case("\n#!skript\n42")
        self.assertIn(verify.FAM_SHEBANG, families)
        self.assertTolerated(families, "misplaced shebang")

    def test_shebang_at_offset_zero_agrees(self):
        _, _, _, families, _ = self.run_case("#!skript\n42")
        self.assertNotIn(verify.FAM_SHEBANG, families)
        self.assertTolerated(families, "shebang at offset 0")

    def test_second_shebang_line_agrees(self):
        # The reader honours `#!` only at offset 0, and so does the grammar
        # (shebang is not an `extra`), so this pair must agree.
        _, _, _, families, _ = self.run_case("#!a\n#!b\n42")
        self.assertTolerated(families, "second shebang line")

    def test_unterminated_block_comment(self):
        source = "#| never closed"
        _, view, _, families, _ = self.run_case(source)
        self.assertIn(
            ("block_comment", 0, len(source)),
            view.comments,
            "the external scanner owns the rest of an unterminated block",
        )
        self.assertIn(verify.FAM_UNTERM, families)
        self.assertTolerated(families, source)

    def test_unterminated_block_after_forms_comments_and_closed_blocks(self):
        # The reader resumes at the end of its last token, so whitespace, a
        # line comment and an earlier CLOSED block may all sit in front of the
        # opener, and an inner `|#` inside a nested block closes nothing.
        for source in (
            "42 #| never closed",
            "42 ; note\n#| never closed",
            "42 #| a |# #| b",
            "#| a #| b |# c",
        ):
            with self.subTest(source=source):
                _, view, _, families, _ = self.run_case(source)
                self.assertTrue(
                    [c for c in view.comments if c[2] == len(source)],
                    "the scanner must keep the rest of the input as a comment",
                )
                self.assertIn(verify.FAM_UNTERM, families)
                self.assertTolerated(families, source)

    def test_a_claim_that_does_not_match_the_opener_is_not_an_opener(self):
        # The tree's own span cannot stand in for the reader's evidence: a
        # block comment starting one byte late is not the opener.
        source = "42 #| never closed"
        _, view, _, families, _ = self.run_case(source)
        self.assertIn(verify.FAM_UNTERM, families)
        shifted = make_view(
            entries=view.entries,
            comments=[(kind, start + 1, end) for kind, start, end in view.comments],
            missing=view.missing,
            errors=view.errors,
            unknown=view.unknown,
        )
        broken = self.families_for(source, shifted, [])
        self.assertNotIn(verify.FAM_UNTERM, broken)
        self.assertIn(verify.FAM_ERR, broken)

    def test_a_closed_block_before_another_failure_is_not_an_opener(self):
        for source in ('42 #| a |# 43 "x', "42 #b2", '42 "abc'):
            with self.subTest(source=source):
                _, _, _, families, _ = self.run_case(source)
                self.assertNotIn(verify.FAM_UNTERM, families)
                self.assertFatal(families, source)

    def test_a_closer_after_a_datum_marker_agrees_on_both_sides(self):
        # `#;)` is a marker and a live `)`, and the derived span is the marker
        # alone - a closer where a form must start is not a form.
        for source in ("#;)", "#;]", "#;}", "#;#;)", "#;#;) 42"):
            with self.subTest(source=source):
                _, view, _, families, _ = self.run_case(source)
                self.assertNotIn(verify.FAM_OPAQUE_SPAN, families, source)
                self.assertNotIn(verify.FAM_MISMATCH, families, source)
                self.assertTolerated(families, source)

    def test_the_prefix_in_front_of_a_failed_number_is_compared(self):
        for source in ("(a) 42abc", "#;1 2 :foo 42abc"):
            with self.subTest(source=source):
                _, view, _, families, _ = self.run_case(source)
                self.assertIn(verify.FAM_NUM, families, source)
                self.assertTolerated(families, source)
        # The tree really did lex past the failure. Those leaves are the
        # unobserved tail, and the marker span the reader DID fix is checked.
        _, view, _, _, _ = self.run_case("#;1 2 :foo 42abc")
        self.assertIn(("number", 11, 13), view.entries)
        self.assertIn(("identifier", 13, 16), view.entries)

    def test_a_corrupted_prefix_behind_a_tolerated_failure_is_fatal(self):
        source = "#;1 2 :foo 42abc"
        _, view, _, families, _ = self.run_case(source)
        self.assertIn(verify.FAM_NUM, families)
        self.assertTolerated(families, source)
        over = self.families_for(source, view, [(0, 5)])
        self.assertIn(verify.FAM_OPAQUE_SPAN, over)
        self.assertFatal(over, source)

    def test_a_rejection_inside_a_swallowed_value_is_not_a_span_mismatch(self):
        # The reader rejects the value the scanner swallowed, so the marker owns
        # bytes the oracle never determined. The claim is neither trusted nor
        # compared - how far the comment runs is the scanner's recovery, and
        # the harness has no evidence about it - while the reader's own
        # rejection still stands as an unclassified failure.
        for source in ("#; 42abc", '#; "abc', "#;#; #b2", "#;#;1 2abc"):
            with self.subTest(source=source):
                _, _, _, families, _ = self.run_case(source)
                self.assertNotIn(verify.FAM_OPAQUE_SPAN, families, source)
                self.assertNotIn(verify.FAM_MISMATCH, families, source)
                self.assertIn(verify.FAM_UNDETERMINED, families, source)
                self.assertIn(verify.FAM_ERR, families, source)
                self.assertFatal(families, source)

    def test_a_value_the_reader_finished_is_still_checked(self):
        # These the reader DID finish, so the marker owns exactly the span its
        # token stream implies - a quote chain, a lone value, and a value with a
        # live prefix behind it - and a claim one byte longer is fatal.
        for source, span in (
            ("#;'1 2abc", (0, 4)),
            ("#;1 2abc", (0, 3)),
            ("#;1 2 :foo 42abc", (0, 3)),
        ):
            with self.subTest(source=source):
                _, view, _, families, spans = self.run_case(source)
                self.assertEqual([span], spans, source)
                self.assertIn(verify.FAM_NUM, families, source)
                self.assertTolerated(families, source)
                over = self.families_for(source, view, [(span[0], span[1] + 1)])
                self.assertIn(verify.FAM_OPAQUE_SPAN, over)
                self.assertFatal(over, source)

    def test_a_chain_returning_an_unclosed_collection_agrees(self):
        # `#;#;1 (2` lexes cleanly - there is no failure to excuse anything -
        # and the chain's second form is a collection the input never closes.
        # A complete read ends at end of input, so the marker owns the rest of
        # the source: the tree has to claim exactly that, and nothing may stay
        # live behind it.
        source = "#;#;1 (2"
        oracle, view, _, families, spans = self.run_case(source)
        self.assertEqual("ok", oracle["status"], source)
        self.assertEqual([(0, 8)], spans, source)
        self.assertEqual([("datum_comment", 0, 8)], view.entries, source)
        self.assertTolerated(families, source)

    def test_unterminated_string_is_never_swallowed(self):
        _, _, _, families, _ = self.run_case('"abc')
        self.assertIn(verify.FAM_ERR, families)

    def test_bad_radix_is_never_swallowed(self):
        _, _, _, families, _ = self.run_case("#b2")
        self.assertIn(verify.FAM_ERR, families)

    def test_trailing_dot_number_is_never_swallowed(self):
        _, _, _, families, _ = self.run_case("1.")
        self.assertIn(verify.FAM_ERR, families)

    def test_zero_width_keyword_is_never_swallowed(self):
        _, _, _, families, _ = self.run_case(":(")
        self.assertFatal(families, ":(")

    def test_identifier_keeps_an_embedded_quote(self):
        # Token.zig:252-264 terminates an identifier only on whitespace or
        # ()[]{};" - never on `'` - so `x'y` is ONE symbol to the reader, and
        # the grammar's identifier body has to agree.
        _, view, _, families, _ = self.run_case("x'y")
        self.assertEqual([("identifier", 0, 3)], view.entries)
        self.assertEqual([], families)

    def test_a_brace_alone_waives_nothing(self):
        # A real, unrelated reader rejection in a source that happens to carry a
        # `{` must stay unclassified, and any curly-infix entry for it must be
        # the informational one with brace evidence attached.
        source = '{a "unterminated}'
        _, _, divergences, families, _ = self.run_case(source)
        self.assertIn(verify.FAM_ERR, families)
        self.assertFatal(families, source)
        for div in divergences:
            if div.family == verify.FAM_CURLY:
                self.assertTrue(
                    div.detail.get("informational"),
                    "curly-infix waiver without brace-region evidence",
                )

    def test_unbalanced_brace_never_becomes_a_plain_mismatch(self):
        _, _, _, families, _ = self.run_case("(a {1 2)")
        self.assertNotIn(verify.FAM_MISMATCH, families)
        self.assertTolerated(families, "unbalanced brace")

    def test_well_formed_sources_produce_no_divergence(self):
        for source in (
            "()",
            "(f 1 2 3)",
            "{1 + 2}",
            ":{ :a 1 :b 2 }",
            "(def x 1)",
            "(defn f (a b) (+ a b))",
            "(fn (a) a)",
            "(defn (f a) a)",
            "#b1010 #o777 #d99 #x1f",
            "1e-10 4e-3i inf.0+2.0i",
            '"a\\\nb"',
            "'(1 2)",
            "#;42",
            "; c\n42",
            "#| a |# 42",
            "héllo nil? #true",
        ):
            _, _, _, families, _ = self.run_case(source)
            self.assertEqual([], families, f"{source!r} produced {families}")


class OpaqueSpanTests(VerifierTestBase):
    """The datum_comment span comes from the lexer, not from the tree."""

    def reader_stream(self, source: str) -> list:
        data = source.encode("utf-8")
        oracle = verify.run_oracle(self.tools, [self.write("op", data)])[0]
        self.assertEqual("ok", oracle["status"], source)
        return verify.reader_view(oracle["tokens"], data)

    def derived(self, source: str):
        return verify.collapse_datum_comments(self.reader_stream(source))

    def derived_spans(self, source: str) -> list:
        return self.derived(source).spans

    def collapsed(self, source: str) -> list:
        return self.derived(source).entries

    def test_marker_collapses_one_read_value(self):
        # Reader.zig:355-363: the marker discards ONE form and returns the
        # next, so the span stops at the discarded value.
        self.assertEqual([(0, 3)], self.derived_spans("#;1 2 3"))

    def test_marker_owns_a_whole_collection(self):
        self.assertEqual([(0, 7)], self.derived_spans("#;(1 2) 3"))

    def test_marker_owns_a_quoted_value(self):
        self.assertEqual([(0, 4)], self.derived_spans("#;'1 2"))

    def test_nested_marker_matches_reader_zig(self):
        # `#;#;1 2 3` owns `#;#;1 2`; `3` is the following live token.
        self.assertEqual([(0, 7)], self.derived_spans("#;#;1 2 3"))

    def test_quote_before_a_marker_stays_live(self):
        # `'#;1 2` is a quote, an opaque `#;1`, then the live `2`.
        stream = self.derived("'#;1 2")
        self.assertEqual([(1, 4)], stream.spans)
        self.assertEqual(
            [("quote", 0, 1), ("datum_comment", 1, 4), ("number", 5, 6)],
            stream.entries,
        )

    def test_marker_inside_a_collection_is_reached(self):
        # `(f #;(g 1) 2)` - the marker is live inside the call, not skipped.
        stream = self.derived("(f #;(g 1) 2)")
        self.assertEqual([(3, 10)], stream.spans)
        self.assertEqual(
            [
                ("lparen", 0, 1),
                ("identifier", 1, 2),
                ("datum_comment", 3, 10),
                ("number", 11, 12),
                ("rparen", 12, 13),
            ],
            stream.entries,
        )

    def test_a_closer_where_a_value_must_start_is_not_a_form(self):
        # In all three families the marker owns itself alone and the closer
        # stays a live token with its own range.
        for source, kind in (
            ("#;)", "rparen"),
            ("#;]", "rbracket"),
            ("#;}", "rbrace"),
        ):
            with self.subTest(source=source):
                self.assertEqual([(0, 2)], self.derived_spans(source))
                self.assertEqual(
                    [("datum_comment", 0, 2), (kind, 2, 3)], self.collapsed(source)
                )

    def test_a_nested_marker_before_a_closer_owns_both_markers(self):
        # `#;#;)` is bytes 0..4 and nothing more; the `)` opens no form.
        self.assertEqual([(0, 4)], self.derived_spans("#;#;)"))
        self.assertEqual(
            [("datum_comment", 0, 4), ("rparen", 4, 5)], self.collapsed("#;#;)")
        )

    def test_a_live_form_after_the_closer_keeps_its_range(self):
        self.assertEqual([(0, 4)], self.derived_spans("#;#;) 42"))
        self.assertEqual(
            [
                ("datum_comment", 0, 4),
                ("rparen", 4, 5),
                ("number", 6, 8),
            ],
            self.collapsed("#;#;) 42"),
        )

    def test_a_closer_that_owns_nothing_ends_a_collection_too(self):
        # `#;(1 ] 2)`: the `]` closes neither the `(` nor anything else, so
        # the value ends before it - the same guard one level down.
        self.assertEqual([(0, 4)], self.derived_spans("#;(1 ] 2)"))

    def test_span_boundaries_must_sit_on_token_edges(self):
        reader = self.reader_stream("#;42")
        ok, _ = verify.verify_opaque_span((0, 4), reader)
        self.assertTrue(ok)
        ok, why = verify.verify_opaque_span((0, 3), reader)
        self.assertFalse(ok)
        self.assertIn("straddles a span boundary", why)
        ok, why = verify.verify_opaque_span((1, 4), reader)
        self.assertFalse(ok)
        self.assertIn("no reader token starts", why)
        ok, why = verify.verify_opaque_span((2, 4), reader)
        self.assertFalse(ok)
        self.assertIn("not a datum_comment", why)
        ok, why = verify.verify_opaque_span((4, 4), reader)
        self.assertFalse(ok)
        self.assertIn("empty or inverted", why)

    def test_real_datum_comment_span_is_reported_by_the_probe(self):
        data = b"#;42"
        records, opaque = self.probe(data)
        self.assertEqual([(0, 4)], [(o["s"], o["e"]) for o in opaque])
        view = verify.tree_view(verify.build_tree(records))
        self.assertEqual([("datum_comment", 0, 4)], view.entries)

    def test_parser_over_claiming_a_span_is_caught(self):
        # Feed analyse_case a span one byte longer than the reader's own token
        # stream implies; it must be reported, not quietly accepted.
        data = b"#;42"
        path = self.write("over", data)
        oracle = verify.run_oracle(self.tools, [path])[0]
        nodes, _ = verify.run_probe_tree(self.tools, [path])
        view = verify.tree_view(verify.build_tree(nodes[0]))
        divergences = verify.analyse_case(data, oracle, view, [(0, 5)])
        families = {d.family for d in divergences}
        self.assertIn(verify.FAM_OPAQUE_SPAN, families)
        self.assertFatal(sorted(families), "over-claimed datum span")


class FormWalkTests(unittest.TestCase):
    """What a form is, over token kinds alone - and what a failure leaves open.

    A complete read ends at end of input, so `#;` alone owns just the marker.
    A truncated read does not: the lexer failed, so the end of the emitted
    stream is no boundary at all and a form the reader never finished has no
    end the harness may invent for it.
    """

    STRAY_CLOSERS = (
        ("#;)", [("datum_comment", 0, 2), ("rparen", 2, 3)]),
        ("#;]", [("datum_comment", 0, 2), ("rbracket", 2, 3)]),
        ("#;}", [("datum_comment", 0, 2), ("rbrace", 2, 3)]),
    )

    def test_a_closer_where_a_form_must_start_is_left_unconsumed(self):
        for source, tokens in self.STRAY_CLOSERS:
            with self.subTest(source=source):
                self.assertEqual((1, True), verify.form_stop(tokens, 1))

    def test_a_stray_closer_stays_a_live_token(self):
        for source, tokens in self.STRAY_CLOSERS:
            with self.subTest(source=source):
                stream = verify.collapse_datum_comments(tokens)
                self.assertEqual([(0, 2)], stream.spans)
                self.assertEqual([("datum_comment", 0, 2), tokens[1]], stream.entries)

    def test_a_nested_marker_owns_the_pair_and_not_the_closer(self):
        tokens = [
            ("datum_comment", 0, 2),
            ("datum_comment", 2, 4),
            ("rparen", 4, 5),
        ]
        stream = verify.collapse_datum_comments(tokens)
        self.assertEqual([(0, 4)], stream.spans)
        self.assertEqual([("datum_comment", 0, 4), ("rparen", 4, 5)], stream.entries)

    def test_a_live_form_after_the_closer_keeps_its_range(self):
        tokens = [
            ("datum_comment", 0, 2),
            ("datum_comment", 2, 4),
            ("rparen", 4, 5),
            ("number", 6, 8),
        ]
        self.assertEqual(
            [("datum_comment", 0, 4), ("rparen", 4, 5), ("number", 6, 8)],
            verify.collapse_datum_comments(tokens).entries,
        )

    def test_a_closer_that_owns_nothing_ends_a_collection(self):
        tokens = [
            ("datum_comment", 0, 2),
            ("lparen", 2, 3),
            ("number", 3, 4),
            ("rbracket", 5, 6),
            ("number", 7, 8),
            ("rparen", 8, 9),
        ]
        stream = verify.collapse_datum_comments(tokens)
        self.assertEqual([(0, 4)], stream.spans)
        self.assertEqual(
            [
                ("datum_comment", 0, 4),
                ("rbracket", 5, 6),
                ("number", 7, 8),
                ("rparen", 8, 9),
            ],
            stream.entries,
        )

    def test_a_complete_read_runs_an_open_collection_to_end_of_input(self):
        # `#;(1 2` lexes fine - it is the reader's PARSER that rejects it - and
        # the value it owns runs to the end of the input.
        tokens = [
            ("datum_comment", 0, 2),
            ("lparen", 2, 3),
            ("number", 3, 4),
            ("number", 5, 6),
        ]
        stream = verify.collapse_datum_comments(tokens)
        self.assertEqual([(0, 6)], stream.spans)
        self.assertIsNone(stream.undetermined)

    def test_a_chain_returning_an_unclosed_collection_owns_the_rest(self):
        # `#;#;1 (2`: the chain owes two forms, and the second is a collection
        # the input never closes. A COMPLETE read ends at end of input, so the
        # marker owns what is left of the source and nothing stays live behind
        # it; the same walk after a failure reaches an end nobody fixed.
        tokens = [
            ("datum_comment", 0, 2),
            ("datum_comment", 2, 4),
            ("number", 4, 5),
            ("lparen", 6, 7),
            ("number", 7, 8),
        ]
        read = verify.collapse_datum_comments(tokens)
        self.assertEqual([(0, 8)], read.spans)
        self.assertEqual([("datum_comment", 0, 8)], read.entries)
        self.assertIsNone(read.undetermined)
        failed = verify.collapse_datum_comments(tokens, truncated=True)
        self.assertEqual([], failed.spans)
        self.assertEqual(0, failed.undetermined)

    # (source, the tokens the lexer emitted before it died, the span a
    # COMPLETE read of the same stream derives). Every one of these has a
    # datum value the reader never got to finish, so a failed read derives
    # nothing at all - while a complete read still ends at end of input.
    LEFT_OPEN = (
        # A lone marker whose value produced no token at all: `42abc` is not a
        # number to the reader, and an unterminated string is not a string.
        ("#; 42abc", [("datum_comment", 0, 2)], (0, 2)),
        ('#; "abc', [("datum_comment", 0, 2)], (0, 2)),
        # A nested chain that runs out before the value the inner marker owes.
        (
            "#;#; #b2",
            [("datum_comment", 0, 2), ("datum_comment", 2, 4)],
            (0, 4),
        ),
        # A nested pair that emitted only ONE of the two forms the chain owes,
        # so the marker owns bytes past the last token it ever reached.
        (
            "#;#;1 2abc",
            [("datum_comment", 0, 2), ("datum_comment", 2, 4), ("number", 4, 5)],
            (0, 5),
        ),
        # A collection the failure cut in half.
        (
            "#;(1 2abc",
            [
                ("datum_comment", 0, 2),
                ("lparen", 2, 3),
                ("number", 3, 4),
                ("number", 5, 6),
            ],
            (0, 6),
        ),
    )

    # The mirror image: the value WAS emitted whole, so the marker owns a span
    # the reader fixed even though the lexer died later in the input.
    FIXED = (
        # A quote chain - `'` is a prefix, and its operand is a complete token.
        (
            "#;'1 2abc",
            [("datum_comment", 0, 2), ("quote", 2, 3), ("number", 3, 4)],
            (0, 4),
        ),
        # A lone marker whose value the reader emitted whole.
        (
            "#;1 2abc",
            [("datum_comment", 0, 2), ("number", 2, 3)],
            (0, 3),
        ),
        # An ordinary value with a live prefix behind it and the failure well
        # past both.
        (
            "#;1 2 :foo 42abc",
            [
                ("datum_comment", 0, 2),
                ("number", 2, 3),
                ("number", 4, 5),
                ("keyword", 7, 10),
            ],
            (0, 3),
        ),
    )

    def test_a_value_the_failure_never_finished_owns_nothing(self):
        for source, tokens, complete in self.LEFT_OPEN:
            with self.subTest(source=source):
                failed = verify.collapse_datum_comments(tokens, truncated=True)
                self.assertEqual([], failed.spans)
                self.assertEqual([], failed.entries)
                self.assertEqual(0, failed.undetermined)
                # Read to end of input the same stream IS a boundary, so the
                # marker owns what it owns and no end is invented.
                read = verify.collapse_datum_comments(tokens)
                self.assertEqual([complete], read.spans)
                self.assertIsNone(read.undetermined)

    def test_a_value_the_reader_did_finish_stays_determined(self):
        for source, tokens, span in self.FIXED:
            with self.subTest(source=source):
                stream = verify.collapse_datum_comments(tokens, truncated=True)
                self.assertEqual([span], stream.spans)
                self.assertIsNone(stream.undetermined)
                limit = verify.determined_limit(stream, tokens)
                self.assertLessEqual(span[1], limit)

    def test_nothing_the_reader_side_kept_reaches_past_the_limit(self):
        for tokens in (
            [("datum_comment", 0, 2), ("number", 2, 3), ("rparen", 3, 4)],
            [("datum_comment", 0, 2), ("lparen", 2, 3), ("number", 3, 4)],
            [("datum_comment", 0, 2), ("number", 2, 3), ("number", 4, 5)],
        ):
            with self.subTest(tokens=tokens):
                stream = verify.collapse_datum_comments(tokens, truncated=True)
                limit = verify.determined_limit(stream, tokens)
                for entry in stream.entries:
                    self.assertLessEqual(entry[2], limit)


class UnterminatedBlockTests(unittest.TestCase):
    """Where the reader's unterminated `#|` is, and what may not stand in."""

    def opener(self, source: bytes, resume: int):
        return verify.unterminated_block_opener(source, resume)

    def classify(self, source, reader, comments, error=verify.READER_UNEXPECTED_TOKEN):
        return verify.classify_reader_error(
            len(source) - 1, reader, make_view(comments=comments), source, error
        )

    def test_the_opener_survives_whitespace_comments_and_closed_blocks(self):
        self.assertEqual(3, self.opener(b"42 #| never closed", 2))
        self.assertEqual(10, self.opener(b"42 ; note\n#| never closed", 2))
        self.assertEqual(11, self.opener(b"42 #| a |# #| b", 2))
        self.assertEqual(22, self.opener(b"#!/usr/bin/env skript\n#| b", 0))

    def test_the_resume_point_is_the_last_token_end(self):
        # A scan of the whole source would have found the `#|` at byte 3; the
        # reader only ever looks on from the byte it stopped at, so the block
        # after `43` is the one that is unterminated.
        self.assertEqual(14, self.opener(b"42 #| a |# 43 #| b", 13))

    def test_an_inner_close_does_not_close_the_outer_block(self):
        self.assertEqual(0, self.opener(b"#| a #| b |# c", 0))
        self.assertEqual(0, self.opener(b"#| a #| b #| c |# d |# e", 0))
        self.assertEqual(3, self.opener(b"42 #| #| |#", 2))
        self.assertIsNone(self.opener(b"#| a #| b |# c |# 42", 0))
        self.assertIsNone(self.opener(b"42 #| #| |# |#", 2))

    def test_nothing_else_is_an_opener(self):
        for source, resume in (
            (b'42 "abc', 2),  # an unterminated string
            (b"42 #b2", 2),  # a bad radix
            (b"42 #| a |# 43", 2),  # the block closed
            (b"42 ; #| not a block\nx", 2),  # inside a line comment
            (b"42", 2),  # the failure is not in this source at all
        ):
            with self.subTest(source=source):
                self.assertIsNone(self.opener(source, resume))

    def test_a_matching_claim_is_classified(self):
        source = b"42 #| never closed"
        family, detail = self.classify(
            source, [("number", 0, 2)], [("block_comment", 3, len(source))]
        )
        self.assertEqual(verify.FAM_UNTERM, family)
        self.assertEqual(3, detail["opener"])
        self.assertEqual(2, detail["last_token_end"])

    def test_only_the_readers_own_unexpected_token_may_be_explained(self):
        source = b"42 #| never closed"
        for error in ("Stuck", "OutOfMemory", "Unimplemented", "unexpectedToken"):
            with self.subTest(error=error):
                family, _ = self.classify(
                    source,
                    [("number", 0, 2)],
                    [("block_comment", 3, len(source))],
                    error,
                )
                self.assertEqual(verify.FAM_ERR, family)

    def test_the_claim_must_start_at_the_opener_and_run_to_end_of_input(self):
        source = b"42 #| never closed"
        for comments in (
            [("block_comment", 4, len(source))],  # one byte late
            [("block_comment", 3, len(source) - 1)],  # stops short of the end
            [("line_comment", 3, len(source))],  # not a block comment
            [("block_comment", 0, len(source))],  # starts before the resume point
        ):
            with self.subTest(comments=comments):
                family, _ = self.classify(source, [("number", 0, 2)], comments)
                self.assertEqual(verify.FAM_ERR, family)


class ErrorPathTests(unittest.TestCase):
    """A tolerated failure must not waive the prefix the reader did determine."""

    @staticmethod
    def cases() -> list:
        """(source, oracle record, the tree's own reading, its opaque claims)."""
        return [
            (
                b"(a) 42abc",
                failure(
                    [
                        ("lparen", 0, 1),
                        ("identifier", 1, 2),
                        ("rparen", 2, 3),
                        ("number", 3, 5),
                    ],
                    5,
                ),
                [
                    ("lparen", 0, 1),
                    ("identifier", 1, 2),
                    ("rparen", 2, 3),
                    ("number", 3, 5),
                    ("identifier", 5, 8),
                ],
                [],
            ),
            (
                b"#;1 2 :foo 42abc",
                failure(
                    [
                        ("datum_comment", 0, 2),
                        ("number", 2, 3),
                        ("number", 4, 5),
                        ("keyword", 7, 10),
                    ],
                    13,
                ),
                [
                    ("datum_comment", 0, 3),
                    ("number", 4, 5),
                    ("keyword", 6, 10),
                    ("number", 11, 13),
                    ("identifier", 13, 16),
                ],
                [(0, 3)],
            ),
        ]

    def families(self, source, oracle, entries=(), spans=(), errors=(), unknown=()):
        view = make_view(entries=entries, errors=errors, unknown=unknown)
        divergences = verify.analyse_case(source, oracle, view, list(spans))
        return [div.family for div in divergences]

    def test_an_agreeing_prefix_leaves_only_the_tolerated_failure(self):
        for source, oracle, entries, spans in self.cases():
            with self.subTest(source=source):
                families = self.families(source, oracle, entries, spans=spans)
                self.assertEqual([verify.FAM_NUM], families)

    def test_a_dropped_prefix_token_is_fatal(self):
        for source, oracle, entries, spans in self.cases():
            with self.subTest(source=source):
                families = self.families(source, oracle, entries[1:], spans=spans)
                self.assertIn(verify.FAM_MISMATCH, families)
                self.assertTrue(unclassified(families), families)

    def test_a_shifted_prefix_range_is_fatal(self):
        for source, oracle, entries, spans in self.cases():
            with self.subTest(source=source):
                kind, start, end = entries[1]
                broken = [entries[0], (kind, start, end + 1), *entries[2:]]
                families = self.families(source, oracle, broken, spans=spans)
                self.assertTrue(unclassified(families), families)

    def test_a_corrupted_opaque_claim_is_fatal(self):
        source, oracle, entries, _ = self.cases()[1]
        families = self.families(source, oracle, entries, spans=[(0, 5)])
        self.assertIn(verify.FAM_OPAQUE_SPAN, families)
        self.assertTrue(unclassified(families), families)

    def test_an_unknown_leaf_inside_the_prefix_is_fatal(self):
        source, oracle, entries, spans = self.cases()[1]
        without = [e for e in entries if e != ("keyword", 6, 10)]
        families = self.families(
            source,
            oracle,
            without,
            spans=spans,
            unknown=[("mystery", 6, 10)],
        )
        self.assertIn(verify.FAM_LEAF, families)
        self.assertTrue(unclassified(families), families)

    def test_an_unknown_leaf_in_the_unobserved_tail_is_not_evidence(self):
        for source, oracle, entries, spans in self.cases():
            with self.subTest(source=source):
                families = self.families(
                    source,
                    oracle,
                    entries,
                    spans=spans,
                    unknown=[("mystery", len(source) - 3, len(source))],
                )
                self.assertNotIn(verify.FAM_LEAF, families)
                self.assertEqual([verify.FAM_NUM], families)

    def test_a_value_the_failure_cut_in_half_is_reported_not_assumed(self):
        # `#;(1 2abc`: the scanner owns the rest of the input, the reader
        # emitted only `1`, and nothing here can confirm or refute that span.
        source = b"#;(1 2abc"
        oracle = failure(
            [
                ("datum_comment", 0, 2),
                ("lparen", 2, 3),
                ("number", 3, 4),
                ("number", 5, 6),
            ],
            6,
        )
        families = self.families(
            source,
            oracle,
            entries=[("datum_comment", 0, 9)],
            spans=[(0, 9)],
        )
        self.assertIn(verify.FAM_UNDETERMINED, families)
        self.assertNotIn(verify.FAM_OPAQUE_SPAN, families)
        self.assertNotIn(verify.FAM_MISMATCH, families)
        # The reader still rejects the number the swallowed form contains, and
        # nothing here waives that.
        self.assertTrue(unclassified(families), families)

    # The reader rejects a value the scanner swallowed whole, so the marker owns
    # bytes the oracle never emitted. The claim's END is deliberately not the
    # one any reader could derive: nothing at or past the limit is compared, so
    # every one of these claims behaves the same.
    REJECTED = (
        (
            b"#; 42abc",
            failure([("datum_comment", 0, 2)], 6),
            [("datum_comment", 0, 8)],
            (0, 8),
        ),
        (
            b'#; "abc',
            failure([("datum_comment", 0, 2)], 6),
            [("datum_comment", 0, 7)],
            (0, 7),
        ),
        (
            b"#;#; #b2",
            failure(
                [("datum_comment", 0, 2), ("datum_comment", 2, 4)],
                7,
            ),
            [("datum_comment", 0, 8)],
            (0, 8),
        ),
        # A nested chain that emitted one form of the two it owes: the rest of
        # the input is the chain's own, and the reader never got there.
        (
            b"#;#;1 2abc",
            failure(
                [
                    ("datum_comment", 0, 2),
                    ("datum_comment", 2, 4),
                    ("number", 4, 5),
                ],
                7,
            ),
            [("datum_comment", 0, 10)],
            (0, 10),
        ),
    )

    def test_a_rejected_value_is_only_a_reader_error(self):
        for source, oracle, entries, claim in self.REJECTED:
            with self.subTest(source=source):
                families = self.families(source, oracle, entries=entries, spans=[claim])
                self.assertIn(verify.FAM_ERR, families)
                self.assertNotIn(verify.FAM_OPAQUE_SPAN, families)
                self.assertNotIn(verify.FAM_MISMATCH, families)
                # The claim is reported as unverified rather than trusted, and
                # the reader's own rejection still stands.
                self.assertIn(verify.FAM_UNDETERMINED, families)
                self.assertTrue(unclassified(families), families)

    # The value WAS emitted, so the span is the reader's to compare and a
    # mutation of it is a real finding.
    FIXED_PREFIX = (
        (
            b"#;'1 2abc",
            failure(
                [("datum_comment", 0, 2), ("quote", 2, 3), ("number", 3, 4)],
                6,
            ),
            [("datum_comment", 0, 4), ("number", 5, 6), ("identifier", 6, 9)],
            (0, 4),
            (0, 5),
        ),
        (
            b"#;1 2abc",
            failure(
                [("datum_comment", 0, 2), ("number", 2, 3)],
                5,
            ),
            [("datum_comment", 0, 3), ("number", 4, 5), ("identifier", 5, 8)],
            (0, 3),
            (0, 4),
        ),
    )

    def test_a_fixed_prefix_agrees_and_stays_checked(self):
        for source, oracle, entries, claim, mutant in self.FIXED_PREFIX:
            with self.subTest(source=source):
                families = self.families(source, oracle, entries=entries, spans=[claim])
                self.assertEqual([verify.FAM_NUM], families)
                broken = self.families(source, oracle, entries=entries, spans=[mutant])
                self.assertIn(verify.FAM_OPAQUE_SPAN, broken)
                self.assertTrue(unclassified(broken), broken)

    def test_a_brace_region_stops_where_the_oracle_stopped(self):
        # `(f { 42abc`: the `{` never closes, but the region may not run past
        # the bytes the reader determined, or an ERROR node out in the
        # unobserved tail would license the curly-infix waiver for a
        # difference inside the braces.
        source = b"(f { 42abc"
        oracle = failure(
            [
                ("lparen", 0, 1),
                ("identifier", 1, 2),
                ("lbrace", 3, 4),
                ("number", 5, 7),
            ],
            7,
        )
        entries = [("lparen", 0, 1), ("identifier", 1, 2), ("number", 5, 7)]
        beyond = self.families(source, oracle, entries, errors=[("ERROR", 7, 10)])
        self.assertIn(verify.FAM_MISMATCH, beyond)
        self.assertNotIn(verify.FAM_CURLY, beyond)
        within = self.families(source, oracle, entries, errors=[("ERROR", 3, 7)])
        self.assertIn(verify.FAM_CURLY, within)
        self.assertEqual([], unclassified(within))


class BraceRegionTests(unittest.TestCase):
    def test_regions_come_from_the_oracle_tokens(self):
        reader = [
            ("lparen", 0, 1),
            ("lbrace", 2, 3),
            ("rbrace", 6, 7),
            ("rparen", 7, 8),
        ]
        self.assertEqual([], verify.unbalanced_brace_regions(reader, 8))
        self.assertEqual(
            [(2, 7)], verify.unbalanced_brace_regions(reader[:2] + reader[3:], 7)
        )

    def test_hunk_must_lie_inside_the_region(self):
        regions = [(10, 20)]
        view = make_view(errors=[("ERROR", 12, 12)])
        self.assertIsNone(verify.brace_region_for(1, 5, regions, view))
        self.assertEqual((10, 20), verify.brace_region_for(11, 15, regions, view))

    def test_error_witness_must_overlap_the_hunk(self):
        regions = [(0, 200)]
        far = make_view(errors=[("ERROR", 100, 100)])
        self.assertIsNone(
            verify.brace_region_for(10, 13, regions, far),
            "an ERROR far from the hunk must not license it",
        )
        near = make_view(missing=[("}", 12, 12)])
        self.assertEqual((0, 200), verify.brace_region_for(10, 13, regions, near))


class CorpusTests(VerifierTestBase):
    def test_deterministic_corpus_never_stalls_the_lexer(self):
        for case in verify.deterministic_cases():
            path = self.write("corpus", case.source.encode("utf-8"))
            oracle = verify.run_oracle(self.tools, [path])[0]
            self.assertNotEqual("io_error", oracle["status"], case.name)
            self.assertNotEqual("Stuck", oracle.get("error"), case.name)
            self.assertEqual(
                len(case.source.encode("utf-8")), oracle["source_len"], case.name
            )

    def test_generated_corpus_is_deterministic(self):
        first = verify.generated_cases(64, 20261002)
        second = verify.generated_cases(64, 20261002)
        self.assertEqual(first, second)
        self.assertEqual(64, len(first))
        self.assertNotEqual(first, verify.generated_cases(64, 7))

    def test_skript_corpus_is_discovered(self):
        sources = verify.skript_sources(DEFAULT_SKRIPT)
        self.assertGreaterEqual(len(sources), 30, "expected the live .pt corpus")
        self.assertTrue(all(p.suffix == ".pt" for p in sources))


TRANSITION_ANCHORS = {
    "delimiter/insert-rparen": ")",
    "quote/open": "42",
    "comment/nest-open": "#|",
    "string/escape": '"',
}


class IncrementalTests(VerifierTestBase):
    @staticmethod
    def node_record(**overrides) -> dict:
        """One node record shaped like the probe's, for the comparator tests."""
        record = {
            "depth": 1,
            "type": "call",
            "field": "callee",
            "s": 1,
            "e": 2,
            "sr": 0,
            "sc": 1,
            "er": 0,
            "ec": 2,
            "named": 1,
            "missing": 0,
            "error": 0,
            "extra": 0,
            "has_error": 0,
            "children": 0,
        }
        record.update(overrides)
        return record

    def test_reuse_matches_a_fresh_parse(self):
        work = self.tools.work / "incremental-selftest"
        saw_missing = False
        saw_field = False
        for label, base in verify.INCREMENTAL_BASES:
            files, edits, applied = verify.build_edit_chain(work, label, base, 20)
            self.assertTrue(edits, f"{label} produced no edits")
            stages, opaque = verify.run_probe_incremental(self.tools, files, edits)
            labels = [step.label for step in applied]
            for mutation, anchor in TRANSITION_ANCHORS.items():
                if anchor in base:
                    self.assertIn(mutation, labels, f"{label} never reached {mutation}")
            for stage, sides in sorted(stages.items()):
                diff = verify.compare_node_lists(sides["incremental"], sides["fresh"])
                self.assertIsNone(
                    diff,
                    f"{label} stage {stage} ({applied[stage].label}) differs at "
                    f"node {diff[0]} field {diff[1]}: {diff[4]!r} vs {diff[5]!r}"
                    if diff
                    else "",
                )
                for rec in sides["fresh"]:
                    saw_missing = saw_missing or bool(rec["missing"])
                    saw_field = saw_field or rec["field"] is not None
                self.assertEqual(
                    verify.spans_of(opaque.get(stage, {}).get("incremental", [])),
                    verify.spans_of(opaque.get(stage, {}).get("fresh", [])),
                    f"{label} stage {stage} opaque spans differ",
                )
        self.assertTrue(
            saw_missing, "no stage exercised the grammar's MISSING recovery"
        )
        self.assertTrue(saw_field, "no stage exercised a named child field")

    def test_a_difference_in_extra_or_has_error_is_a_divergence(self):
        for field in ("extra", "has_error"):
            with self.subTest(field=field):
                diff = verify.compare_node_lists(
                    [self.node_record()], [self.node_record(**{field: 1})]
                )
                if diff is None:
                    self.fail(f"a difference in {field} was not compared")
                self.assertEqual(field, diff[1])

    def test_unequal_node_counts_report_a_length_divergence(self):
        first = self.node_record()
        second = self.node_record(type="identifier", field=None, s=2, e=3, sc=2, ec=3)
        for left, right, side in (
            ([first], [first, second], "fresh"),
            ([first, second], [first], "incremental"),
        ):
            with self.subTest(extra_side=side):
                diff = verify.compare_node_lists(left, right)
                if diff is None:
                    self.fail("trees with different node counts must never agree")
                self.assertEqual("length", diff[1])
                self.assertEqual(1, diff[0], "index of the first unmatched node")
                self.assertEqual((len(left), len(right)), (diff[2], diff[3]))
                self.assertEqual(side, diff[5]["side"])

    def test_a_record_missing_a_compared_field_is_refused(self):
        # Two records that BOTH lack a field are the case that used to slip
        # through: read with `.get`, they compared equal.
        holed = self.node_record()
        del holed["extra"]
        for label, left, right, side in (
            ("both sides", holed, dict(holed), "incremental"),
            ("one side", self.node_record(), dict(holed), "fresh"),
        ):
            with self.subTest(case=label):
                with self.assertRaises(verify.HarnessError) as caught:
                    verify.compare_node_lists([left], [right])
                message = str(caught.exception)
                self.assertIn(side, message)
                self.assertIn("node 0", message)
                self.assertIn("extra", message)


if __name__ == "__main__":
    unittest.main(verbosity=2)
