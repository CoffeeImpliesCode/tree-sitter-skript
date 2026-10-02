#!/usr/bin/env python3
"""Differential and incremental verification for tree-sitter-skript.

Two independent oracles, one command:

    python3 test/verify.py differential --skript ../skript
    python3 test/verify.py incremental

`differential` lexes every input with the REAL Skript lexer and parses the same
bytes with the REAL generated tree-sitter parser, then compares leaf tokens and
byte ranges token by token. `incremental` checks that reusing a tree through
`ts_tree_edit` produces the same tree as parsing the edited text from scratch.

Neither mode re-implements either side:

  * the lexer oracle compiles a byte-for-byte copy of `<skript>/src/Token.zig`
    (see `test/lexer_root.zig` for the two declarations it borrows from
    skript's root, and `stage_lexer` below for the hash check that proves the
    copy is the live source);
  * the parser probe compiles the checked-in `src/parser.c` plus the required
    `src/scanner.c`, against an installed tree-sitter runtime.

WORK DIRECTORY OWNERSHIP
-----------------------
The `--work-dir` path (or `$TREE_SITTER_SKRIPT_VERIFY_WORKDIR`, or the default)
is a PARENT, never a scratch to write into. Every run creates exactly one
fresh child of it, puts everything it builds inside that child, and removes
only that child - on success AND on failure. Nothing already living in the
parent is read, overwritten or deleted, so a caller can keep its own files
there. The parent is resolved through symlinks before it is created and
re-checked afterwards, and anything resolving inside /tmp or /var/tmp is
refused.

DIVERGENCE FAMILIES
-------------------
A difference is only tolerated when it matches one of these families, and each
family demands concrete evidence drawn from the two oracles - never from the
mere presence of some token in the input. Anything else is UNCLASSIFIED and
fails the run.

number_terminator_leniency
    The reader rejects a number whose next byte is not whitespace, a grouping
    delimiter or EOF (`../skript/src/Token.zig:364-369`); the grammar reads it
    as a number plus an identifier. Evidence: the oracle failed with
    UnexpectedToken at offset B, the offending byte src[B] is not a terminator
    (or B is end of input), and the tree's `num_lit` leaf ends exactly at B.

misplaced_shebang
    `#!` is a shebang only at byte offset 0 (`Token.zig:190-195`); anywhere
    else the reader lexes `#!...` as one identifier. Evidence: the differing
    reader entry is an identifier starting with `#!`, and the tree dropped a
    `shebang` node covering exactly the same byte range.

curly_infix_ahead_of_reader
    The reader's parser answers error.Unimplemented for a bare `{`, so the
    grammar is deliberately ahead of it. Classified ONLY when every differing
    entry, and the ERROR/MISSING node that explains it, sit inside one specific
    unbalanced-brace region computed from the ORACLE's own lbrace/rbrace tokens.
    An input merely containing a `{` waives nothing.

unterminated_block_comment_editor_recovery
    An unterminated `#|` opener makes the reader fail (`Token.zig:216-231`).
    The editor grammar keeps the rest of the file as a comment instead.
    Evidence: the oracle failed with UnexpectedToken, the first byte at or
    after its last successful token that is not a CLOSED extra opens a `#|`
    whose nesting never balances before end of input, and the tree dropped a
    `block_comment` node covering exactly that opener to end of input. The
    failure offset is NOT evidence - `skipBlockComment` gives up on the last
    byte, not at the opener - and a `#|` anywhere else in the source says
    nothing at all.

NORMALIZATION RULES
-------------------
1. Reader keywords re-attach their colon. `lexKeyword` eats `:` BEFORE
   recording the token start (`Token.zig:237-239`), so a reader keyword token
   starts one byte after the `:` it belongs to. The harness moves the start back
   and asserts src[start-1] == ':' - a failed assert is a harness bug, not a
   divergence.
2. `bool_lit` and `nil_lit` fold down to `identifier`, because `#t`, `#f` and
   `nil` are ordinary identifiers to the reader (`Token.zig` lexIdentifier).
3. The anonymous `def`, `defn` and `fn` tokens fold down to `identifier`; the
   reader has no special forms at all.
4. `line_comment`, `block_comment` and `shebang` leaves are dropped from the
   tree side, because the reader skips those bytes without emitting a token.
   They are kept in a side table, which is what the misplaced-shebang and
   unterminated-comment classifiers read as evidence.
5. Zero-width MISSING nodes are dropped. They are the grammar's permissive
   recovery for forms the reader's parser would reject, and they claim no
   bytes, so dropping them cannot hide consumed text. They are counted and
   their spans reported.
6. A `#;` datum comment collapses to ONE entry on each side, spanning
   `marker.start .. discarded_value.end`. The span is derived from the
   ORACLE'S TOKEN STREAM alone - `Reader.zig:340-410` `readFormNode` is
   reimplemented over token kinds only, never over source text and never from
   the tree's claim - and the derived span is then compared against the opaque
   span the parser reports. A longer claim is over-consumption and is reported
   as `opaque_span_mismatch`. The derived span is additionally checked to start
   exactly on a `#;` token and end exactly on a token edge, so it cannot hide
   code the lexer saw outside the form.
7. Tree leaves whose type has no entry in TREE_LEAF_KINDS are reported, not
   skipped, so a new grammar token cannot quietly bypass comparison.
8. When the lexer fails, the failure itself is classified and the comparison
   CONTINUES over the prefix the reader did determine: the tokens it emitted,
   the datum spans they independently imply, and the leaves the tree put
   there. Everything from the end of its last successful token on is the
   unobserved tail - the tree lexed bytes the reader never reached, so those
   bytes cannot constrain a difference - and so is a datum value the reader
   never finished: one that produced no token at all, or one the failure cut
   in half. Either way the boundary moves back to the marker that owns it. A
   tolerated family therefore never waives a corrupted prefix, and the
   informational grammar-error note is printed only when the tokens agree.
   A claim that crosses that boundary is reported as
   `datum_span_not_determined` rather than passing as checked-and-clean.

Exit status: 0 when every difference was classified, 1 when any difference was
not, 2 on a usage or environment problem.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import difflib
import glob
import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_DIR = REPO_ROOT / "test"
SRC_DIR = REPO_ROOT / "src"

# The shared scratch this project is allowed to use. Never /tmp or /var/tmp.
SHARED_WORK_DIR = Path("/home/tmp2/omp/tree-sitter-skript-work")
FORBIDDEN_WORK_ROOTS = (Path("/tmp"), Path("/var/tmp"))

# Ownership token for the per-run child directory. A run creates exactly one
# child of the caller's parent and deletes only a child carrying this prefix;
# `remove_run_dir` refuses anything else, so a caller can never lose a file
# this harness did not create in this run.
RUN_DIR_PREFIX = "run-"

# --------------------------------------------------------------------------
# Divergence families
# --------------------------------------------------------------------------

FAM_NUM = "number_terminator_leniency"
FAM_SHEBANG = "misplaced_shebang"
FAM_CURLY = "curly_infix_ahead_of_reader"
FAM_UNTERM = "unterminated_block_comment_editor_recovery"
# Informational: reported with evidence, never fatal, never a waiver.
FAM_CURLY_SHAPE = "grammar_error_outside_braces"
FAM_UNDETERMINED = "datum_span_not_determined"
# Fatal.
FAM_ERR = "unclassified_reader_error"
FAM_MISMATCH = "unclassified_token_mismatch"
FAM_OPAQUE = "datum_span_boundary_violation"
FAM_OPAQUE_SPAN = "opaque_span_mismatch"
FAM_LEAF = "unclassified_leaf_type"
FAM_INCR = "unclassified_incremental_mismatch"
FAM_HARNESS = "harness_error"

CLASSIFIED_FAMILIES = (FAM_NUM, FAM_SHEBANG, FAM_CURLY, FAM_UNTERM)
INFORMATIONAL_FAMILIES = (FAM_CURLY_SHAPE, FAM_UNDETERMINED)
FATAL_FAMILIES = (
    FAM_ERR,
    FAM_MISMATCH,
    FAM_OPAQUE,
    FAM_OPAQUE_SPAN,
    FAM_LEAF,
    FAM_INCR,
    FAM_HARNESS,
)

# Per-case detail printed before the report collapses into a count. One root
# cause can hit hundreds of generated cases; the family counts stay exact.
MAX_REPORTED_CASES = 20

# Number terminators, verbatim from `../skript/src/Token.zig:364-369`.
NUMBER_TERMINATORS = b" \t\r\n(){}[];"

# Tree-sitter leaf type -> the reader kind it is normalized to. A leaf type
# that is missing here is reported rather than skipped (rule 7).
TREE_LEAF_KINDS = {
    "(": "lparen",
    ")": "rparen",
    "[": "lbracket",
    "]": "rbracket",
    "{": "lbrace",
    "}": "rbrace",
    ".": "period",
    "'": "quote",
    # `map_marker: _ => token(/:\{/)` is a NAMED rule, so the leaf's type is
    # `map_marker`, not the anonymous `:{` spelling. Both are mapped.
    "map_marker": "map_marker",
    ":{": "map_marker",
    "#;": "datum_comment",
    # The reader has no special forms: `def`, `defn` and `fn` are symbols.
    "def": "identifier",
    "defn": "identifier",
    "fn": "identifier",
    # `bool_lit: _ => choice('#t', '#f')` and `nil_lit: _ => 'nil'` are hidden
    # rules, so the leaves carry the literal spellings. The reader lexes all
    # three as ordinary identifiers, so they normalize down to `identifier`.
    "bool_lit": "identifier",
    "nil_lit": "identifier",
    "#t": "identifier",
    "#f": "identifier",
    "nil": "identifier",
    "identifier": "identifier",
    "num_lit": "number",
    "string_lit": "string",
    "kwd_lit": "keyword",
}

# Bytes the reader skips without producing a token.
COMMENT_NODE_TYPES = ("line_comment", "block_comment", "shebang")

# Nodes whose whole span is one opaque token. The tree claims the range; the
# verifier proves it against the lexer output before believing it (rule 6).
OPAQUE_NODE_TYPES = ("datum_comment",)

NODE_COMPARE_FIELDS = (
    "depth",
    "type",
    "field",
    "s",
    "e",
    "sr",
    "sc",
    "er",
    "ec",
    "named",
    "missing",
    "error",
    "extra",
    "has_error",
    "children",
)


class HarnessError(RuntimeError):
    """A problem with the harness itself, not a grammar/reader divergence."""


class WorkDirError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

Entry = tuple  # (kind, start, end) with byte offsets


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def show_bytes(data: bytes, limit: int = 72) -> str:
    """Render source bytes for an evidence line without ever being ambiguous."""
    if len(data) > limit:
        return repr(data[:limit]) + f"...(+{len(data) - limit}B)"
    return repr(data)


def overlaps(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    if a_start == a_end or b_start == b_end:
        return a_start <= b_start < b_end or b_start <= a_start < b_end
    return a_start < b_end and b_start < a_end


# --------------------------------------------------------------------------
# Work directory and subprocess environment
# --------------------------------------------------------------------------


def _reject_forbidden(path: Path, what: str) -> None:
    for root in FORBIDDEN_WORK_ROOTS:
        real_root = Path(os.path.realpath(root))
        if path == real_root or real_root in path.parents:
            raise WorkDirError(
                f"refusing {what} {path}: it resolves inside {real_root}, which is "
                "shared and volatile. Pass --work-dir pointing at a project-owned "
                "or cache-owned path."
            )


def resolve_work_dir(explicit: str | os.PathLike | None) -> Path:
    """Resolve the PARENT directory a run may put its own child inside.

    Precedence: --work-dir, then $TREE_SITTER_SKRIPT_VERIFY_WORKDIR, then the
    XDG cache home, then the project-shared scratch. The result is fully
    symlink-resolved and refused outright if it lands inside /tmp or /var/tmp.
    Nothing is created, written or removed here: this is only the place the
    run's own private child will live.
    """
    if explicit is not None:
        candidate = Path(explicit)
    elif os.environ.get("TREE_SITTER_SKRIPT_VERIFY_WORKDIR"):
        candidate = Path(os.environ["TREE_SITTER_SKRIPT_VERIFY_WORKDIR"])
    else:
        xdg = os.environ.get("XDG_CACHE_HOME")
        base = Path(xdg) if xdg else Path.home() / ".cache"
        candidate = base / "tree-sitter-skript" / "verify"
        if not base.is_dir() or not os.access(base, os.W_OK):
            candidate = SHARED_WORK_DIR
    candidate = Path(candidate).expanduser()
    if not candidate.is_absolute():
        candidate = (Path.cwd() / candidate).resolve()
    resolved = Path(os.path.realpath(candidate))
    _reject_forbidden(resolved, "work directory")
    return resolved


def create_run_dir(parent: Path) -> Path:
    """Create the one child directory this run owns, and return it.

    The parent belongs to the caller, so the run touches nothing in it beyond
    creating it when it is missing: everything built afterwards goes into a
    fresh `mkdtemp` child. The parent is resolved through symlinks BEFORE it is
    created and re-checked afterwards, because `mkdir` follows symlinks and the
    safety check has to describe what is really on disk rather than the
    spelling the caller typed.
    """
    parent = Path(os.path.realpath(parent))
    _reject_forbidden(parent, "work directory")
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise WorkDirError(f"cannot create work directory {parent}: {exc}") from exc
    real_parent = Path(os.path.realpath(parent))
    if not real_parent.is_dir():
        raise WorkDirError(f"work directory {real_parent} is not a directory")
    _reject_forbidden(real_parent, "work directory")
    try:
        run_dir = Path(tempfile.mkdtemp(prefix=RUN_DIR_PREFIX, dir=real_parent))
    except OSError as exc:
        raise WorkDirError(
            f"cannot create a run directory in {real_parent}: {exc}"
        ) from exc
    resolved = Path(os.path.realpath(run_dir))
    if resolved != run_dir or resolved.parent != real_parent:
        raise WorkDirError(
            f"run directory {run_dir} does not resolve to a fresh child of "
            f"{real_parent} (it resolves to {resolved}); refusing to write there"
        )
    _reject_forbidden(resolved, "run directory")
    return resolved


def remove_run_dir(run_dir: Path) -> None:
    """Delete ONLY the directory this run created.

    Every claim to ownership is checked before anything is unlinked: the path
    must not be a symlink, must carry RUN_DIR_PREFIX, must resolve to itself,
    and must not be one of the forbidden roots. Anything else is refused
    rather than deleted, so a mistyped path can never take caller files with it.
    """
    run_dir = Path(run_dir)
    if run_dir.is_symlink():
        raise WorkDirError(f"refusing to remove {run_dir}: it is a symlink")
    if not run_dir.name.startswith(RUN_DIR_PREFIX):
        raise WorkDirError(
            f"refusing to remove {run_dir}: it does not carry the run-directory "
            f"prefix {RUN_DIR_PREFIX!r}, so this run did not create it"
        )
    resolved = Path(os.path.realpath(run_dir))
    if resolved != run_dir:
        raise WorkDirError(
            f"refusing to remove {run_dir}: it resolves to {resolved}, which is "
            "not the directory this run created"
        )
    if not resolved.is_dir():
        raise WorkDirError(f"refusing to remove {resolved}: it is not a directory")
    _reject_forbidden(resolved, "run directory")
    shutil.rmtree(resolved)


@contextlib.contextmanager
def owned_run_dir(parent: Path, keep: bool = False):
    """Hand a run its own child of `parent`, then remove exactly that child.

    Removal happens in a `finally`, so a run that raises leaves the caller's
    parent exactly as it found it. With `keep` the child stays in place and its
    exact path is printed, because that path is the only thing a caller may
    safely delete itself.
    """
    run_dir = create_run_dir(parent)
    try:
        yield run_dir
    finally:
        if keep:
            print(f"  kept run directory: {run_dir}")
        else:
            try:
                remove_run_dir(run_dir)
            except (WorkDirError, OSError) as exc:
                print(
                    f"verify.py: could not remove run directory {run_dir}: {exc}",
                    file=sys.stderr,
                )


def subprocess_env(work: Path, library_dir: Path | None = None) -> dict:
    """Point every cache and temporary directory inside the run's own child.

    `work` is this run's private directory, so a compiler or a subprocess that
    scribbles in TMPDIR or a cache home still cannot touch the caller's parent.
    """
    env = dict(os.environ)
    for name in ("tmp", "cache", "zig-local", "zig-global"):
        (work / name).mkdir(parents=True, exist_ok=True)
    for var in ("TMPDIR", "TMP", "TEMP"):
        env[var] = str(work / "tmp")
    env["XDG_CACHE_HOME"] = str(work / "cache")
    env["ZIG_LOCAL_CACHE_DIR"] = str(work / "zig-local")
    env["ZIG_GLOBAL_CACHE_DIR"] = str(work / "zig-global")
    if library_dir is not None:
        env["TREE_SITTER_LIBDIR"] = str(library_dir)
        existing = env.get("LD_LIBRARY_PATH", "")
        parts = [str(library_dir)] + [p for p in existing.split(":") if p]
        env["LD_LIBRARY_PATH"] = ":".join(parts)
    return env


def run(cmd: list, env: dict, cwd: Path = REPO_ROOT, verbose: bool = False) -> bytes:
    if verbose:
        print("  $ " + " ".join(str(c) for c in cmd), file=sys.stderr)
    proc = subprocess.run(cmd, env=env, cwd=str(cwd), capture_output=True)
    if proc.returncode != 0:
        raise HarnessError(
            f"command failed ({proc.returncode}): {' '.join(str(c) for c in cmd)}\n"
            f"stdout: {proc.stdout.decode('utf-8', 'replace')[:4000]}\n"
            f"stderr: {proc.stderr.decode('utf-8', 'replace')[:4000]}"
        )
    return proc.stdout


# --------------------------------------------------------------------------
# tree-sitter runtime discovery
# --------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Runtime:
    include_dir: Path  # directory that contains tree_sitter/api.h
    library: Path  # the shared object / dylib itself


def _first_existing(candidates, predicate) -> Path | None:
    for candidate in candidates:
        try:
            if predicate(Path(candidate)):
                return Path(candidate)
        except OSError:
            continue
    return None


def _header_candidates(repo_root: Path) -> list:
    seen = []
    seen += sorted(repo_root.glob("zig-pkg/*/lib/include"))
    seen += sorted(repo_root.glob("zig-pkg/*/include"))
    seen += sorted(repo_root.glob(".zig-cache/p/*/include"))
    seen += [
        Path("/usr/include"),
        Path("/usr/local/include"),
        Path("/opt/homebrew/include"),
    ]
    return seen


def _library_candidates() -> list:
    return [
        Path("/usr/lib64"),
        Path("/usr/lib/x86_64-linux-gnu"),
        Path("/usr/lib"),
        Path("/lib/x86_64-linux-gnu"),
        Path("/usr/local/lib"),
        Path("/opt/homebrew/lib"),
    ]


def discover_runtime(
    include_override: str | None, library_override: str | None
) -> Runtime:
    """Find a usable tree-sitter header + library, preferring explicit overrides.

    There is no pkg-config `tree-sitter.pc` on this machine, so pkg-config is
    tried but never depended on; the installed versioned runtime and a vendored
    header are the fallbacks.
    """
    include_dir = Path(include_override).resolve() if include_override else None
    if (
        include_dir is not None
        and not (include_dir / "tree_sitter" / "api.h").is_file()
    ):
        raise HarnessError(f"--runtime-include {include_dir} has no tree_sitter/api.h")

    library = Path(library_override).resolve() if library_override else None
    if library is not None and not library.is_file():
        raise HarnessError(f"--runtime-library {library} is not a file")

    if library is None:
        found = _first_existing(
            _library_candidates(),
            lambda p: bool(glob.glob(str(p / "libtree-sitter.so*"))),
        )
        if found is None:
            found = _first_existing(
                _library_candidates(),
                lambda p: bool(glob.glob(str(p / "libtree-sitter*.dylib"))),
            )
        if found is None:
            found = _first_existing(
                _library_candidates(),
                lambda p: bool(glob.glob(str(p / "libtree-sitter-*.dll*"))),
            )
        if found is not None:
            library = sorted(glob.glob(str(found / "libtree-sitter.so*")))[0]
            library = Path(library)
        else:
            # Last resort: whatever the dynamic loader knows about.
            import ctypes.util

            name = ctypes.util.find_library("tree-sitter")
            if name:
                for base in _library_candidates():
                    candidate = base / name
                    if candidate.is_file():
                        library = candidate
                        break
    if library is None:
        raise HarnessError(
            "no tree-sitter runtime library found. Pass --runtime-library "
            "/path/to/libtree-sitter.so.0.26 (this machine ships "
            "/usr/lib64/libtree-sitter.so.0.26)."
        )

    if include_dir is None:
        found = _first_existing(
            _header_candidates(REPO_ROOT),
            lambda p: (p / "tree_sitter" / "api.h").is_file(),
        )
        if found is None:
            raise HarnessError(
                "no tree_sitter/api.h found. Pass --runtime-include "
                "/path/to/include (this repository vendors one under zig-pkg/*/lib/include)."
            )
        include_dir = found

    return Runtime(include_dir=include_dir, library=library)


# --------------------------------------------------------------------------
# Building the two oracles
# --------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Tools:
    work: Path
    oracle: Path
    probe: Path
    runtime: Runtime
    skript_root: Path
    token_sha: str
    scanner: Path


def stage_lexer(work: Path, skript_root: Path, verbose: bool) -> tuple[Path, str]:
    """Copy the live Token.zig next to lexer_root.zig, proving it is byte-exact.

    Token.zig resolves `@import("root.zig")` relative to its own directory, so
    the copy MUST be named Token.zig and sit beside a file named root.zig.
    """
    source = skript_root / "src" / "Token.zig"
    if not source.is_file():
        raise HarnessError(
            f"{source} does not exist; pass --skript at a Skript checkout"
        )
    stage = work / "oracle"
    stage.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(TEST_DIR / "lexer_root.zig", stage / "root.zig")
    target = stage / "Token.zig"
    shutil.copyfile(source, target)
    source_digest = sha256(source)
    copied_digest = sha256(target)
    if source_digest != copied_digest:
        raise HarnessError(
            f"staged Token.zig is not byte-identical to {source}: "
            f"{copied_digest} != {source_digest}"
        )
    if verbose:
        print(f"  staged {source} sha256={source_digest}", file=sys.stderr)
    return stage, source_digest


def build_tools(
    work: Path, skript_root: Path, runtime: Runtime, verbose: bool
) -> Tools:
    env = subprocess_env(work, runtime.library.parent)
    stage, token_sha = stage_lexer(work, skript_root, verbose)

    zig = shutil.which("zig")
    if zig is None:
        raise HarnessError("zig is not on PATH; the lexer oracle needs Zig 0.16")
    oracle = work / "bin" / "lexer_oracle"
    oracle.parent.mkdir(parents=True, exist_ok=True)
    run(
        [
            zig,
            "build-exe",
            str(stage / "root.zig"),
            "-O",
            "ReleaseSafe",
            "-femit-bin=" + str(oracle),
            "--cache-dir",
            str(work / "zig-local"),
            "--global-cache-dir",
            str(work / "zig-global"),
        ],
        env,
        verbose=verbose,
    )

    cc = os.environ.get("CC") or shutil.which("cc") or shutil.which("gcc")
    if cc is None:
        raise HarnessError("no C compiler found; the parser probe needs cc or gcc")
    probe = work / "bin" / "parser_probe"
    sources = [str(TEST_DIR / "parser_probe.c"), str(SRC_DIR / "parser.c")]
    scanner = SRC_DIR / "scanner.c"
    if not scanner.is_file():
        # `#|` and `#;` are external tokens (grammar.js:41). Without the
        # scanner the probe would parse neither, and every comparison against
        # it would be meaningless rather than merely incomplete.
        raise HarnessError(
            f"{scanner} is missing; the block comment and datum comment tokens "
            "are external and the probe cannot be built without it"
        )
    sources.append(str(scanner))
    run(
        [cc, "-std=c11", "-O1", "-I", str(SRC_DIR), "-I", str(runtime.include_dir)]
        + sources
        + ["-o", str(probe), str(runtime.library)],
        env,
        verbose=verbose,
    )
    return Tools(
        work=work,
        oracle=oracle,
        probe=probe,
        runtime=runtime,
        skript_root=skript_root,
        token_sha=token_sha,
        scanner=scanner,
    )


# --------------------------------------------------------------------------
# Running the two oracles
# --------------------------------------------------------------------------


def run_oracle(tools: Tools, paths: list, verbose: bool = False) -> list:
    if not paths:
        return []
    env = subprocess_env(tools.work, tools.runtime.library.parent)
    out = run([str(tools.oracle)] + [str(p) for p in paths], env, verbose=verbose)
    records = []
    for line in out.decode("utf-8").splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))
    if len(records) != len(paths):
        raise HarnessError(
            f"lexer oracle returned {len(records)} records for {len(paths)} inputs"
        )
    return records


def run_probe_tree(
    tools: Tools, paths: list, verbose: bool = False
) -> tuple[dict, dict]:
    """Return {case_index: [node records]} plus {case_index: [opaque records]}."""
    if not paths:
        return {}, {}
    env = subprocess_env(tools.work, tools.runtime.library.parent)
    out = run(
        [str(tools.probe), "tree"] + [str(p) for p in paths], env, verbose=verbose
    )
    nodes: dict = {}
    opaque: dict = {}
    for line in out.decode("utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        case = rec["case"]
        if "children" in rec:
            nodes.setdefault(case, []).append(rec)
        else:
            opaque.setdefault(case, []).append(rec)
    if len(nodes) != len(paths):
        raise HarnessError(
            f"parser probe returned {len(nodes)} cases for {len(paths)} inputs"
        )
    return nodes, opaque


def run_probe_incremental(
    tools: Tools, chain: list, edits: list, verbose: bool = False
) -> tuple:
    """Run one edit chain.

    Returns ({stage: {side: [node records]}}, {stage: {side: [opaque records]}})
    so the caller can compare the reused tree against the fresh one, including
    the opaque datum_comment spans each side claims.
    """
    files = [str(p) for p in chain]
    args = [str(tools.probe), "incremental", str(len(edits))]
    args += files
    for start, old_end, new_end in edits:
        args += [str(start), str(old_end), str(new_end)]
    env = subprocess_env(tools.work, tools.runtime.library.parent)
    out = run(args, env, verbose=verbose)

    opaque: dict = {}
    stages: dict = {}
    for line in out.decode("utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        if "children" in rec:
            stages.setdefault(rec["case"], {}).setdefault(rec["parse"], []).append(rec)
        else:
            opaque.setdefault(rec["case"], {}).setdefault(rec["parse"], []).append(rec)
    expected = set(range(len(edits)))
    if set(stages) != expected:
        raise HarnessError(
            f"parser probe returned stages {sorted(stages)}, expected {sorted(expected)}"
        )
    for stage, sides in stages.items():
        if set(sides) != {"incremental", "fresh"}:
            raise HarnessError(
                f"stage {stage} is missing a parse side: {sorted(sides)}"
            )
    return stages, opaque


# --------------------------------------------------------------------------
# Tree reconstruction
# --------------------------------------------------------------------------


@dataclasses.dataclass
class Node:
    type: str
    field: str | None
    start: int
    end: int
    start_point: tuple
    end_point: tuple
    named: bool
    missing: bool
    error: bool
    depth: int
    children: list


def build_tree(records: list) -> Node:
    """Rebuild the node hierarchy from the probe's pre-order flat dump."""
    root = None
    stack: list = []
    previous_depth = -1
    for index, rec in enumerate(records):
        depth = rec["depth"]
        if depth > previous_depth + 1:
            raise HarnessError(
                f"probe record {index} jumps from depth {previous_depth} to {depth}; "
                "the pre-order dump is inconsistent"
            )
        while stack and stack[-1].depth >= depth:
            stack.pop()
        node = Node(
            type=rec["type"],
            field=rec["field"],
            start=rec["s"],
            end=rec["e"],
            start_point=(rec["sr"], rec["sc"]),
            end_point=(rec["er"], rec["ec"]),
            named=bool(rec["named"]),
            missing=bool(rec["missing"]),
            error=bool(rec["error"]),
            depth=depth,
            children=[],
        )
        if stack:
            if stack[-1].depth != depth - 1:
                raise HarnessError(
                    f"probe record {index} at depth {depth} has no parent at "
                    f"depth {depth - 1}; the pre-order dump is inconsistent"
                )
            stack[-1].children.append(node)
        elif root is None:
            root = node
        else:
            raise HarnessError(f"probe record {index} starts a second root")
        stack.append(node)
        previous_depth = depth
    if root is None:
        raise HarnessError("probe returned an empty tree")
    return root


@dataclasses.dataclass
class TreeView:
    entries: list
    comments: list
    missing: list
    errors: list
    unknown: list


def tree_view(root: Node) -> TreeView:
    """Flatten the tree to leaves, applying the documented normalizations."""
    view = TreeView(entries=[], comments=[], missing=[], errors=[], unknown=[])
    opaque_types = set(OPAQUE_NODE_TYPES)
    comment_types = set(COMMENT_NODE_TYPES)
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type in opaque_types:
            # One entry for the whole claimed span; the caller proves the span
            # against the lexer output before comparing it.
            view.entries.append(("datum_comment", node.start, node.end))
            continue
        if node.error:
            view.errors.append((node.type, node.start, node.end))
        if not node.children:
            if node.missing:
                view.missing.append((node.type, node.start, node.end))
                continue
            if node.type in comment_types:
                view.comments.append((node.type, node.start, node.end))
                continue
            kind = TREE_LEAF_KINDS.get(node.type)
            if kind is None:
                view.unknown.append((node.type, node.start, node.end))
                continue
            view.entries.append((kind, node.start, node.end))
            continue
        stack.extend(reversed(node.children))
    view.entries.sort(key=lambda e: (e[1], e[2]))
    return view


def reader_view(tokens: list, src: bytes) -> list:
    """Reader tokens, with the colon re-attached to each keyword token.

    `lexKeyword` (Token.zig:237-239) eats the ':' and only THEN copies the
    cursor as the token start, so a reader keyword begins one byte after its
    colon. The grammar's kwd_lit begins at the colon. Re-attaching is the only
    way to compare ranges, and the assert turns a harness bug into a loud
    failure instead of a phantom divergence.
    """
    entries = []
    for tok in tokens:
        kind = tok["kind"]
        start = tok["start"]
        end = tok["end"]
        if kind == "keyword":
            if start == 0 or src[start - 1 : start] != b":":
                raise HarnessError(
                    f"reader keyword token [{start},{end}) is not preceded by ':'; "
                    "the normalization rule no longer matches Token.zig"
                )
            start -= 1
        entries.append((kind, start, end))
    return entries


# --------------------------------------------------------------------------
# Opaque span verification
# --------------------------------------------------------------------------

# Token kinds that open a collection, and the closer each one expects. The
# reader's map literal `:{ ... }` is closed by `rbrace`, same as a curly infix.
CLOSERS = {
    "lparen": "rparen",
    "lbracket": "rbracket",
    "lbrace": "rbrace",
    "map_marker": "rbrace",
}
OPENERS = frozenset(CLOSERS)
CLOSING_KINDS = frozenset(("rparen", "rbracket", "rbrace"))


def _collection_stop(tokens: list, index: int, *, truncated: bool = False) -> tuple:
    """(exclusive end index, complete) for the collection at `index`.

    Running out of tokens leaves the collection open, and what that MEANS
    depends on the read: on a complete read the stream ends at end of input,
    so the value really does run to the end of the source and that is a
    boundary the reader fixed; after a failure the stream ends wherever the
    lexer gave up, so the end is not a boundary at all.

    A closer that does not match the innermost open form is a real boundary
    either way - the reader's parser rejects the input there - so the walk
    ends on it, which keeps a disagreement visible instead of swallowing the
    rest.
    """
    stack = [CLOSERS[tokens[index][0]]]
    i = index + 1
    while i < len(tokens):
        kind = tokens[i][0]
        if kind in OPENERS:
            stack.append(CLOSERS[kind])
        elif kind in CLOSING_KINDS:
            if not stack or kind != stack[-1]:
                return i, True
            stack.pop()
            if not stack:
                return i + 1, True
        i += 1
    return len(tokens), not truncated


def form_stop(tokens: list, index: int, *, truncated: bool = False) -> tuple:
    """(exclusive end index, complete) for the FORM starting at `index`.

    Mirrors `Reader.zig:340-410` `readFormNode`, using only the token kinds the
    real lexer produced:

      * a collection runs to its matching closer;
      * `'` (Reader.zig:364-405) is a prefix form, so it owns the form it
        quotes;
      * `#;` (Reader.zig:355-363) reads ONE form, discards it, then reads and
        returns the NEXT form, so a datum marker's own form spans
        marker + discarded form + returned form.

    A closing bracket where a FORM must start is not a form. The reader's
    parser rejects that input, so the walk stops WITHOUT consuming the closer
    - the same guard `_collection_stop` applies to a closer it does not own.
    That is what makes `#;#;)` a marker pair owning bytes 0..4 with the `)`
    left live, instead of a span that swallows a bracket no form opened.

    `complete` says the walk reached the form's own boundary. `truncated` says
    the lexer FAILED, so the end of the emitted stream is NOT a boundary: a
    form starting past the last emitted token, a collection with no closer, and
    a marker chain that runs out before the value it still owes are incomplete,
    because the reader never fixed where they end. A complete read keeps end of
    input as a real boundary, so `#;` alone still derives the marker alone.

    A marker's own span runs to the end of the form it RETURNS, which is a
    form inside that span whenever the marker is itself a value. The walk
    therefore propagates that form's end even when the form is not closed: the
    bytes the reader did reach are the marker's, and only `complete` says
    whether that end is one anybody fixed.
    """
    if index >= len(tokens):
        return len(tokens), not truncated
    kind = tokens[index][0]
    if kind in OPENERS:
        return _collection_stop(tokens, index, truncated=truncated)
    if kind in CLOSING_KINDS:
        return index, True
    if kind == "quote":
        return form_stop(tokens, index + 1, truncated=truncated)
    if kind == "datum_comment":
        discarded, complete = form_stop(tokens, index + 1, truncated=truncated)
        if not complete or discarded <= index + 1:
            return discarded, complete
        returned, returned_complete = form_stop(tokens, discarded, truncated=truncated)
        if returned_complete:
            return returned, True
        # The chain owes a form the reader never finished. Keep the end the
        # walk reached - a collection open to the end of the source owns the
        # rest of it - but never claim the reader fixed that end.
        return returned, False
    return index + 1, True


def outermost_spans(spans: list) -> list:
    """Keep only the outermost of a set of possibly nested spans.

    `#; #; 42` nests: the tree reports a datum_comment covering all eight bytes
    and another covering the inner four. The reader's token walk produces only
    the outer span, because consuming it consumes the inner marker, so only the
    outer claim is comparable. A claim that only PARTIALLY overlaps an accepted
    span is kept, so genuine over-consumption is still caught.
    """
    kept = []
    for start, end in sorted(spans):
        if any(k_start <= start and end <= k_end for k_start, k_end in kept):
            continue
        kept.append((start, end))
    return kept


@dataclasses.dataclass
class DatumStream:
    """What the reader's own token stream determines, tree claims aside.

    `entries` and `spans` are the collapsed lists both sides are compared
    through. `undetermined` is the start byte of the first datum value the
    stream left open - a failed read that never fixed where it ends - or None
    when every span ended where the form ends.
    """

    entries: list
    spans: list
    undetermined: int | None


def collapse_datum_comments(tokens: list, *, truncated: bool = False) -> DatumStream:
    """Derive the reader's datum_comment spans from the TOKEN STREAM alone.

    The tree's own claim is never consulted here. Each marker collapses to
    `('datum_comment', marker.start, discarded_value.end)`, so

        #;1 2 3     -> (0, 3)   then 2 and 3 stay live
        #;(1 2) 3   -> (0, 7)   then 3 stays live
        #;\'1 2      -> (0, 4)   then 2 stays live
        #;#;1 2 3   -> (0, 7)   then 3 stays live
        \'#;1 2      -> the quote is live, (1, 4), then 2 stays live
        #;)         -> (0, 2)   then the stray `)` stays live

    Comparing the returned spans against what the parser claims is what
    catches a node that over-consumes its own form.

    `truncated` says the lexer FAILED, so the stream stops at the failure
    rather than at end of input and `form_stop` treats the end of the emitted
    tokens as no boundary at all. A datum value with no emitted value, or one
    whose value the failure cut in half, has no end the reader determined, so
    the walk reports `undetermined` and stops there instead of inventing one.
    On a complete read the same forms run to the end of the input, which is a
    boundary the reader did fix.
    """
    out = []
    spans = []
    undetermined = None
    index = 0
    while index < len(tokens):
        kind, start, end = tokens[index]
        if kind != "datum_comment":
            out.append((kind, start, end))
            index += 1
            continue
        stop, complete = form_stop(tokens, index + 1, truncated=truncated)
        if truncated and not complete:
            undetermined = start
            break
        if stop <= index + 1:
            # A closing bracket, or nothing at all, follows the marker. The
            # reader rejects that; the marker owns only itself.
            stop = index + 1
        last = tokens[stop - 1][2]
        spans.append((start, last))
        out.append(("datum_comment", start, last))
        index = stop
    return DatumStream(entries=out, spans=spans, undetermined=undetermined)


def verify_opaque_span(span, tokens: list) -> tuple:
    """Boundary check for a datum span, run against the lexer output.

    The span must begin exactly on a `#;` marker token, end exactly on some
    token's end, and no token may straddle either boundary. That is what makes
    the derived span trustworthy: nothing it claims crosses a token edge, so it
    cannot be hiding code that the lexer saw outside the form.
    """
    start, end = span
    if start >= end:
        return False, f"empty or inverted span [{start},{end})"
    heads = [t for t in tokens if t[1] == start]
    if not heads:
        return False, f"no reader token starts at the span start {start}"
    if heads[0][0] != "datum_comment":
        return (
            False,
            f"span starts at ({heads[0][0]}, {heads[0][1]}), not a datum_comment",
        )
    for token in tokens:
        if token[1] < start < token[2] or token[1] < end < token[2]:
            return False, (
                f"reader token ({token[0]}, {token[1]}, {token[2]}) straddles a "
                f"span boundary of [{start},{end})"
            )
    if not any(t[2] == end for t in tokens):
        return False, f"no reader token ends at the span end {end}"
    return True, ""


# --------------------------------------------------------------------------
# Brace regions, hunks and reader failures
# --------------------------------------------------------------------------


def unbalanced_brace_regions(reader: list, src_len: int) -> list:
    """Unbalanced `{`/`}` regions, computed from the ORACLE's tokens only."""
    stack = []
    regions = []
    for kind, start, end in reader:
        if kind == "lbrace":
            stack.append(start)
        elif kind == "rbrace":
            if stack:
                stack.pop()
            else:
                regions.append((start, end))
    for start in stack:
        regions.append((start, src_len))
    return regions


def brace_region_for(
    low: int, high: int, regions: list, view: TreeView
) -> tuple | None:
    """A single brace region that fully contains the hunk AND holds the error."""
    for region in regions:
        if not (region[0] <= low and high <= region[1]):
            continue
        witnesses = view.errors + view.missing
        for kind, start, end in witnesses:
            if overlaps(start, end, low, high):
                return region
    return None


def classify_hunk(
    reader_slice: list,
    tree_slice: list,
    src: bytes,
    regions: list,
    view: TreeView,
) -> tuple:
    """Return (family, evidence) for one difference between the two streams."""
    low = min([e[1] for e in reader_slice] + [e[1] for e in tree_slice], default=0)
    high = max([e[2] for e in reader_slice] + [e[2] for e in tree_slice], default=0)

    # misplaced_shebang: every differing reader entry is a `#!` identifier and
    # the tree dropped a shebang node covering exactly that range.
    shebangs = {(c[1], c[2]) for c in view.comments if c[0] == "shebang"}
    if (
        reader_slice
        and all(
            kind == "identifier"
            and src[start : start + 2] == b"#!"
            and (start, end) in shebangs
            for kind, start, end in reader_slice
        )
        and all((start, end) in shebangs for _, start, end in tree_slice)
    ):
        return FAM_SHEBANG, {
            "reader": [list(e) for e in reader_slice],
            "tree": [list(e) for e in tree_slice],
            "shebang_nodes": sorted(shebangs),
        }

    # curly_infix: narrow, oracle-derived, and only when an ERROR/MISSING node
    # inside the SAME region explains the difference.
    region = brace_region_for(low, high, regions, view)
    if region is not None:
        return FAM_CURLY, {
            "reader": [list(e) for e in reader_slice],
            "tree": [list(e) for e in tree_slice],
            "unbalanced_brace_region": list(region),
            "region_text": show_bytes(src[region[0] : region[1]]),
        }

    return FAM_MISMATCH, {
        "reader": [list(e) for e in reader_slice],
        "tree": [list(e) for e in tree_slice],
        "reader_text": [show_bytes(src[s:e]) for _, s, e in reader_slice],
        "tree_text": [show_bytes(src[s:e]) for _, s, e in tree_slice],
    }


# --------------------------------------------------------------------------
# Locating an unterminated block comment
# --------------------------------------------------------------------------

# The reader's own error name: `@errorName` of `error.UnexpectedToken`, the
# one `skipBlockComment` raises (test/lexer_root.zig spells the record out).
# A bad string, a bad radix and a stalled lexer carry names of their own, and
# so does anything this harness ever adds, so only this exact failure may be
# explained away - and even then the bytes still have to be a block comment.
READER_UNEXPECTED_TOKEN = "UnexpectedToken"

# The reader's whitespace set (Token.zig:188).
WHITESPACE_BYTES = b" \t\r\n"
LINE_COMMENT_BYTE = 0x3B  # ';'
NEWLINE_BYTE = 0x0A  # '\n'
SHEBANG = b"#!"
BLOCK_OPEN = b"#|"
BLOCK_CLOSE = b"|#"


def closed_block_end(src: bytes, opener: int) -> int | None:
    """Byte just past the `#|` block opening at `opener`, or None if unclosed.

    The nesting walk is `Token.zig:216-231` byte for byte, and it has to be:
    the whole question is whether the markers balance to end of input, so
    `#| a #| b |# c` is left at depth 1 and is exactly as unterminated as a
    lone `#|`. Searching the tail for any `|#` instead would waive the inner
    close of that input, and searching the whole source for a `#|` would
    waive a comment the reader never reached.
    """
    depth = 1
    index = opener + len(BLOCK_OPEN)
    size = len(src)
    while depth > 0:
        if index + 1 >= size:
            return None
        pair = src[index : index + 2]
        if pair == BLOCK_OPEN:
            depth += 1
            index += 2
        elif pair == BLOCK_CLOSE:
            depth -= 1
            index += 2
        else:
            index += 1
    return index


def skip_reader_extras(src: bytes, index: int) -> int:
    """First byte at or after `index` the reader would not step over.

    Between its last successful token and the byte that killed it, the reader
    only ever skips these (`Token.zig:108-179`): whitespace, a `;` line
    comment, a shebang at offset 0 only, and a CLOSED `#| ... |#`. This is a
    boundary walk, not a second lexer: it recognises no token, it never looks
    past the first byte that is not an extra, and an unterminated `#|` stops it
    there rather than being skipped.
    """
    size = len(src)
    while index < size:
        byte = src[index]
        if byte in WHITESPACE_BYTES:
            index += 1
            continue
        if byte == LINE_COMMENT_BYTE or (
            index == 0 and src[index : index + 2] == SHEBANG
        ):
            while index < size and src[index] != NEWLINE_BYTE:
                index += 1
            continue
        if src[index : index + 2] == BLOCK_OPEN:
            end = closed_block_end(src, index)
            if end is None:
                return index
            index = end
            continue
        return index
    return index


def unterminated_block_opener(src: bytes, resume: int) -> int | None:
    """Where the unterminated `#|` starts, or None when the failure is not one.

    `resume` is the end of the reader's last successful token. Every byte from
    there to the opener is a closed extra the reader stepped over, so the first
    `#|` that does not close before end of input is the opener - and the tree
    has to claim a block comment covering exactly it to end of input.
    """
    index = skip_reader_extras(src, resume)
    if src[index : index + 2] != BLOCK_OPEN:
        return None
    if closed_block_end(src, index) is None:
        return index
    return None


def classify_reader_error(
    offset: int, reader: list, view: TreeView, src: bytes, error_name: str
) -> tuple:
    """Classify a lexer failure, or report it with everything we know.

    `reader` is the normalized token prefix the lexer DID emit, and `offset` is
    where it gave up. The offset is no help for a block comment: it points at
    the last byte of the input, not at the opener, so the opener is derived
    from the reader's own last token plus the extras it would have skipped.
    """
    # number_terminator_leniency: the grammar's num_lit ends exactly where the
    # reader gave up, and the byte it choked on is not a legal terminator.
    for kind, start, end in view.entries:
        if kind == "number" and end == offset:
            nxt = src[offset : offset + 1]
            if offset >= len(src) or nxt not in NUMBER_TERMINATORS:
                return FAM_NUM, {
                    "offset": offset,
                    "number": [start, end, show_bytes(src[start:end])],
                    "offending_byte": show_bytes(nxt),
                    "rule": "../skript/src/Token.zig:364-369",
                }

    # unterminated_block_comment_editor_recovery.
    resume = reader[-1][2] if reader else 0
    opener = unterminated_block_opener(src, resume)
    if error_name == READER_UNEXPECTED_TOKEN and opener is not None:
        covering = [
            c
            for c in view.comments
            if c[0] == "block_comment" and c[1] == opener and c[2] == len(src)
        ]
        if covering:
            return FAM_UNTERM, {
                "opener": opener,
                "last_token_end": resume,
                "comment_span": [covering[0][1], covering[0][2]],
                "rest_of_input": show_bytes(src[opener:]),
                "rule": "../skript/src/Token.zig:216-231",
            }

    return FAM_ERR, {
        "offset": offset,
        "byte_at_offset": show_bytes(src[offset : offset + 1]),
        "tokens_before_failure": len(reader),
        "context": show_bytes(src[max(0, offset - 24) : offset + 24]),
        "tree_entries": [list(e) for e in view.entries[-8:]],
    }


# --------------------------------------------------------------------------
# Per-case analysis
# --------------------------------------------------------------------------


@dataclasses.dataclass
class Divergence:
    family: str
    detail: dict


def determined_limit(stream: DatumStream, reader: list) -> int:
    """First byte the oracle's own evidence does not determine.

    The reader emitted tokens up to here and no further: past the end of its
    last one the lexer failed, so the tree's reading of those bytes constrains
    nothing and a difference there is not evidence of anything. The limit also
    moves BACK to the marker of a datum value the failure cut in half, because
    a span crossing the failure is not a span the reader determined. The
    failure OFFSET is never used: it points at the byte the lexer choked on,
    which sits inside the number it never finished emitting.
    """
    limit = reader[-1][2] if reader else 0
    if stream.undetermined is not None:
        limit = min(limit, stream.undetermined)
    return limit


def truncate_view(view: TreeView, limit: int) -> TreeView:
    """The same tree without a single node reaching past `limit`.

    `end <= limit` and not `start < limit`: every leaf the reader determined
    lies inside a token it emitted, so nothing determined is dropped, while a
    node that straddles into the undetermined region - the parser's own
    copy of a datum claim is the one that can - is not evidence of anything
    the oracle said.
    """
    return TreeView(
        entries=[e for e in view.entries if e[2] <= limit],
        comments=[c for c in view.comments if c[2] <= limit],
        missing=[m for m in view.missing if m[2] <= limit],
        errors=[e for e in view.errors if e[2] <= limit],
        unknown=[u for u in view.unknown if u[2] <= limit],
    )


def analyse_case(src: bytes, oracle: dict, view: TreeView, opaque: list) -> list:
    divergences = []
    if oracle["status"] == "io_error":
        return [Divergence(FAM_HARNESS, {"io_error": oracle.get("error")})]

    tokens = oracle["tokens"]
    reader = reader_view(tokens, src)
    failed = oracle["status"] == "error"
    stream = collapse_datum_comments(reader, truncated=failed)
    collapsed = stream.entries
    derived_spans = stream.spans
    determined = view
    limit = len(src)
    if failed:
        # The family below explains the FAILURE, not the prefix the reader did
        # determine, so the comparison carries on over that prefix. Everything
        # the tree found at or after the limit is its own reading of bytes the
        # lexer never reached and is not evidence of a difference.
        family, detail = classify_reader_error(
            oracle["offset"], reader, view, src, oracle["error"]
        )
        detail = dict(detail)
        detail["lexer_error"] = oracle["error"]
        divergences.append(Divergence(family, detail))
        limit = determined_limit(stream, reader)
        determined = truncate_view(view, limit)
        # A claim reaching past the limit spans bytes the reader never fixed,
        # so there is nothing to check it against; a claim inside the limit
        # still has to match the independently derived span exactly.
        unchecked = [span for span in opaque if span[1] > limit]
        opaque = [span for span in opaque if span[1] <= limit]
        if unchecked:
            divergences.append(
                Divergence(
                    FAM_UNDETERMINED,
                    {
                        "unverified_claims": [list(s) for s in sorted(unchecked)],
                        "determined_up_to": limit,
                        "incomplete_value_at": stream.undetermined,
                        "note": "the reader never emitted the form these claims "
                        "cover, so nothing here confirms or refutes them; "
                        "every other claim in this case was checked",
                    },
                )
            )

    for span in derived_spans:
        ok, why = verify_opaque_span(span, reader)
        if not ok:
            divergences.append(
                Divergence(
                    FAM_OPAQUE,
                    {
                        "span": list(span),
                        "reason": why,
                        "text": show_bytes(src[span[0] : span[1]]),
                        "note": "the reader-derived datum span failed its own "
                        "boundary invariant; this is a harness bug, not a "
                        "grammar difference",
                    },
                )
            )

    tree_entries = determined.entries
    claimed = outermost_spans(list(opaque))
    if sorted(derived_spans) != sorted(claimed):
        divergences.append(
            Divergence(
                FAM_OPAQUE_SPAN,
                {
                    "reader_derived_spans": [list(s) for s in sorted(derived_spans)],
                    "parser_claimed_spans": [list(s) for s in sorted(claimed)],
                    "parser_claimed_spans_all": [list(s) for s in sorted(opaque)],
                    "note": "the parser's opaque datum_comment span does not "
                    "match the span the reader's token stream implies; a longer "
                    "claim is over-consumption",
                },
            )
        )
        # Report the span difference once, in the family above, and keep
        # comparing every other token rather than double-counting.
        collapsed = [e for e in collapsed if e[0] != "datum_comment"]
        tree_entries = [e for e in tree_entries if e[0] != "datum_comment"]

    # An unclosed `{` must not reach past what the oracle determined either:
    # a region running to end of input would let an ERROR node out in the
    # unobserved tail license a waiver for a difference inside the braces.
    regions = unbalanced_brace_regions(collapsed, limit)

    if collapsed != tree_entries:
        matcher = difflib.SequenceMatcher(a=collapsed, b=tree_entries, autojunk=False)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                continue
            family, detail = classify_hunk(
                collapsed[i1:i2], tree_entries[j1:j2], src, regions, determined
            )
            detail = dict(detail)
            detail["reader_slice"] = [i1, i2]
            detail["tree_slice"] = [j1, j2]
            divergences.append(Divergence(family, detail))

    for leaf_type, start, end in determined.unknown:
        divergences.append(
            Divergence(
                FAM_LEAF,
                {
                    "leaf_type": leaf_type,
                    "span": [start, end],
                    "text": show_bytes(src[start:end]),
                    "note": "no entry in TREE_LEAF_KINDS; add an explicit mapping",
                },
            )
        )

    # What is left below is a NOTE about the tree's own shape. It is printed
    # only while the two sides' tokens agree, so it can never sit on top of a
    # real difference and read as an explanation of one.
    if any(div.family in FATAL_FAMILIES for div in divergences):
        return divergences

    # Informational: the tree shape around an unbalanced `{`. Reported only
    # when the two sides' tokens agree, never fatal, and never a waiver for
    # anything above.
    brace_shaped = 0
    outside = []
    for kind, start, end in view.errors:
        if any(region[0] <= start and end <= region[1] for region in regions):
            brace_shaped += 1
        else:
            outside.append([kind, start, end, show_bytes(src[start:end])])
    if brace_shaped:
        divergences.append(
            Divergence(
                FAM_CURLY,
                {
                    "informational": True,
                    "error_nodes_inside_unbalanced_braces": brace_shaped,
                    "regions": [list(r) for r in regions],
                },
            )
        )
    if outside:
        divergences.append(
            Divergence(
                FAM_CURLY_SHAPE,
                {
                    "error_nodes": outside,
                    "note": "reported, not fatal: a lexer-only oracle cannot say "
                    "whether the reader's parser accepts the construct",
                },
            )
        )
    return divergences


# --------------------------------------------------------------------------
# Corpora
# --------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Case:
    name: str
    source: str
    group: str


# The nested `#| ... |#` and unterminated `#|` cases encode the reader contract
# the external scanner owns, and the `#;` cases the datum span it must derive.
# A closer where a datum value must start, and a lexer failure inside a value
# the scanner swallowed, are the two shapes where the reader's token stream
# alone decides the span - see `form_stop`.
DETERMINISTIC_CASES: list = [
    # --- delimiters -------------------------------------------------------
    ("delim/parens_empty", "()"),
    ("delim/parens_nested", "((()))"),
    ("delim/brackets_empty", "[]"),
    ("delim/brackets", "[1 2 3]"),
    ("delim/braces_empty", "{}"),
    ("delim/period", "."),
    ("delim/period_dotted", "(a . b)"),
    ("delim/mixed", "(a [b] {c} . d)"),
    ("delim/unbalanced_paren", "(a"),
    ("delim/dangling_dot", "(a .)"),
    # --- numbers ----------------------------------------------------------
    ("num/int", "42"),
    ("num/zero", "0"),
    ("num/float", "1.5"),
    ("num/exponent", "1e10 1E10 1e+10 1e-10"),
    ("num/imaginary", "2i"),
    ("num/real_imag", "1+2i 1-2i 1.5+2.5i"),
    ("num/imag_exponent", "4e-3i"),
    ("num/special_float", "inf.0 nan.0 -inf.0 +inf.0"),
    ("num/special_float_pair", "inf.0+2.0i"),
    ("num/special_float_pair_tight", "nan.0-1.0i"),
    ("num/radix", "#b1010 #o777 #d99 #x1f #xDEADbeef"),
    ("num/signed", "-1 +1"),
    ("num/in_list", "(f 1 2 3.5 -4)"),
    ("num/terminator_leniency", "42abc"),
    ("num/terminator_underscore", "1_000"),
    ("num/terminator_quote", '1"abc"'),
    # The failing number is not the only thing after a failure: the list in
    # front of it was emitted and is still compared.
    ("num/terminator_after_list", "(a) 42abc"),
    # --- strings ----------------------------------------------------------
    ("str/empty", '""'),
    ("str/plain", '"hello world"'),
    ("str/escaped_quote", '"a\\"b"'),
    ("str/escaped_backslash", '"a\\\\b"'),
    ("str/escaped_newline", '"a\\\nb"'),
    ("str/in_list", '(print "hi")'),
    # --- keywords and maps -------------------------------------------------
    ("kwd/plain", ":foo"),
    ("kwd/punct_body", ":foo.bar :a-b :+ :*"),
    ("kwd/hash_body", ":#b1010"),
    ("map/marker", ":{ :a 1 :b 2 }"),
    ("map/empty", ":{ }"),
    ("map/in_list", "(m :{:k v})"),
    # --- identifiers -------------------------------------------------------
    ("id/simple", "foo"),
    ("id/dotted", "foo.bar.baz"),
    ("id/symbols", "+ - * / < > = ! _"),
    ("id/at_and_colon", "@rgba ffi:make-func"),
    ("id/dash", "dynamic-library:open"),
    ("id/bool_nil", "#t #f nil"),
    ("id/lookalikes", "#true #false nil? nilx #t"),
    ("id/unicode", "héllo wörld"),
    ("id/tab_indent", "\t(f 1)"),
    # --- quotes ------------------------------------------------------------
    ("quote/atom", "'x"),
    ("quote/list", "'(1 2)"),
    ("quote/nested", "''x"),
    # --- comments ----------------------------------------------------------
    ("comment/line", "; hello\n42"),
    ("comment/line_eof", "; hello"),
    ("comment/line_only", ";"),
    ("comment/block", "#| hello |# 42"),
    ("comment/block_tight", "#|a|#"),
    ("comment/block_multiline", "#| a\nb |# 42"),
    ("comment/block_nested", "#| a #| b |# c |# 42"),
    ("comment/block_unterminated", "#| never closed"),
    # The reader resumes at the end of its last token and steps over closed
    # extras, so an unterminated block is still located when whitespace, a line
    # comment or an earlier CLOSED block sits in front of it, and when only the
    # inner marker of a nested one closes.
    ("comment/block_unterminated_after_form", "42 #| never closed"),
    ("comment/block_unterminated_after_line_comment", "42 ; note\n#| never closed"),
    ("comment/block_unterminated_after_closed_block", "42 #| a |# #| b"),
    ("comment/block_unterminated_outer_nested", "#| a #| b |# c"),
    # A closing bracket where a datum value must start is not a form: the
    # marker owns itself alone and the closer stays a live token. The nested
    # pair owns both markers, and a form after the closer keeps its range.
    ("comment/datum_stray_paren", "#;)"),
    ("comment/datum_stray_bracket", "#;]"),
    ("comment/datum_stray_brace", "#;}"),
    ("comment/datum_nested_stray_closer", "#;#;)"),
    ("comment/datum_nested_stray_closer_then_form", "#;#;) 42"),
    ("comment/datum", "#;42"),
    ("comment/datum_nested", "#; #; 42"),
    ("comment/datum_list", "(f #;(g 1) 2)"),
    ("comment/datum_then_code", "#;42\n(f 1)"),
    # The chain owes two forms and the second is a collection the input never
    # closes. The lexer reads all of it, so a complete read ends at end of
    # input: the marker owns the rest of the source and nothing stays live.
    ("comment/datum_nested_open_collection", "#;#;1 (2"),
    # The lexer dies mid-number, so only the prefix it emitted is comparable:
    # the marker span it fixed is still checked, the number it never finished
    # is not.
    ("comment/datum_terminator_leniency", "#;1 2 :foo 42abc"),
    # --- shebangs ----------------------------------------------------------
    ("shebang/offset_zero", "#!/usr/bin/env skript\n42"),
    ("shebang/second_line", "#!a\n#!b\n42"),
    ("shebang/mid_line", "a #!b c"),
    ("shebang/misplaced", "\n#!skript\n42"),
    # --- forms -------------------------------------------------------------
    ("form/def", "(def x 1)"),
    ("form/def_no_value", "(def x)"),
    ("form/def_trailing", "(def x 1 2 3)"),
    ("form/def_no_name", "(def 1)"),
    ("form/defn_modern", "(defn f (a b) (+ a b))"),
    ("form/defn_degraded", "(defn f a b)"),
    ("form/defn_legacy", "(defn (f a) a)"),
    ("form/fn_params", "(fn (a) a)"),
    ("form/fn_bare", "(fn rest rest)"),
    ("form/fn_empty", "(fn)"),
    ("form/array_of_calls", "[(f 1) (g 2)]"),
    ("form/curly_infix", "{1 + 2}"),
    ("form/curly_nested", "{(f 1) [2] {3}}"),
    ("form/curly_symbols", "{a < b}"),
    ("form/crlf", "(f 1)\r\n(g 2)\r\n"),
    ("form/multi_line", "(def x 1)\n(defn f (a) a)\n(fn (a) a)\n"),
]


def deterministic_cases() -> list:
    return [Case(name, source, "deterministic") for name, source in DETERMINISTIC_CASES]


# The generator concatenates well-formed fragments. It deliberately does not
# emit unterminated strings, stray radix prefixes, `:(` or `x'y`, because those
# are reader rejections the four classified families do not cover; a generated
# case that produces an unclassified difference is a real finding, so the pool
# stays inside the well-formed surface rather than papering over it.
GENERATOR_FRAGMENTS = [
    "42",
    "1.5",
    "1e10",
    "2i",
    "1+2i",
    "inf.0",
    "nan.0",
    "#x1f",
    "#b1010",
    "#o777",
    '"text"',
    '"a\\"b"',
    '"a\\\\b"',
    ":key",
    ":a-b",
    ":+",
    ":*",
    "foo",
    "foo.bar",
    "+",
    "<",
    "=",
    "nil",
    "#t",
    "#f",
    "nil?",
    "#true",
    "héllo",
    "(f 1)",
    "(g x y)",
    "(a . b)",
    "[1 2]",
    "{1 + 2}",
    ":{:k v}",
    "(def x 1)",
    "(defn f (a) a)",
    "(fn (a) a)",
    "#;42",
    "; c\n",
    "#| c |#",
    "#| a #| b |# c |#",
    "'(1 2)",
    "()",
    "[]",
    ":{ :k v }",
]


def generated_cases(count: int, seed: int) -> list:
    rng = random.Random(seed)
    cases = []
    for i in range(count):
        pieces = [rng.choice(GENERATOR_FRAGMENTS) for _ in range(rng.randint(1, 8))]
        cases.append(Case(f"gen/{i:05d}", " ".join(pieces), "generated"))
    return cases


def skript_sources(skript_root: Path) -> list:
    skip = {
        ".zig-cache",
        ".jj",
        "zig-out",
        "node_modules",
        ".git",
        ".devenv",
        ".direnv",
    }
    found = []
    for path in sorted(skript_root.rglob("*.pt")):
        if any(part in skip for part in path.parts):
            continue
        found.append(path)
    return found


# --------------------------------------------------------------------------
# Differential mode
# --------------------------------------------------------------------------


def run_differential(tools: Tools, work: Path, cases: list, verbose: bool) -> int:
    # `work` is this run's own child directory, so it starts empty and is
    # removed wholesale when the run ends. Nothing here deletes anything it
    # did not just create, and nothing outside `work` is touched.
    case_dir = work / "cases"
    case_dir.mkdir(parents=True, exist_ok=True)

    paths = []
    for index, case in enumerate(cases):
        path = case_dir / f"{index:06d}.pt"
        path.write_bytes(case.source.encode("utf-8", "surrogateescape"))
        paths.append(path)

    oracle_records = run_oracle(tools, paths, verbose)
    node_records, opaque_records = run_probe_tree(tools, paths, verbose)

    counts: dict = {}
    failures = []
    fatal_total = 0
    tolerated = set(CLASSIFIED_FAMILIES) | set(INFORMATIONAL_FAMILIES)
    for index, case in enumerate(cases):
        src = case.source.encode("utf-8", "surrogateescape")
        oracle = oracle_records[index]
        if oracle["status"] != "io_error" and oracle.get("source_len") != len(src):
            raise HarnessError(
                f"{case.name}: oracle read {oracle.get('source_len')} bytes, "
                f"expected {len(src)}"
            )
        root = build_tree(node_records[index])
        view = tree_view(root)
        opaque = [(o["s"], o["e"]) for o in opaque_records.get(index, [])]
        divergences = analyse_case(src, oracle, view, opaque)

        fatal = False
        for div in divergences:
            counts[div.family] = counts.get(div.family, 0) + 1
            if div.family not in tolerated:
                fatal = True
        if fatal:
            fatal_total += 1
        if divergences or verbose:
            failures.append((case, src, oracle, view, opaque, divergences, fatal))

    report(tools, "differential", cases, counts, failures, verbose)
    print(f"  unclassified cases: {fatal_total}")
    return 1 if fatal_total else 0


def cases_source(case: Case) -> bytes:
    return case.source.encode("utf-8", "surrogateescape")


# --------------------------------------------------------------------------
# Incremental mode
# --------------------------------------------------------------------------


INCREMENTAL_BASES = [
    (
        "mixed",
        '(f 42)\n#| c |#\n"s"\n#;42\n[1 2]\n{1 + 2}\n:{:k 42}\n',
    ),
    (
        "forms",
        "(def x 42)\n(defn f (a) a)\n(fn (a) 42)\n",
    ),
    (
        "comments",
        "; line\n#| a #| b |# c |# 42\n#;42\n(f 42)\n",
    ),
    (
        "quote_and_string",
        '\'(f 42)\n(f "a\\"b")\n#| q |# 42\n',
    ),
]


def _mutate_insert(anchor: str, text: str, where: str):
    def apply(source: str):
        at = source.find(anchor)
        if at < 0:
            return None
        if where == "before":
            start, old_end = at, at
        else:
            start, old_end = at + len(anchor), at + len(anchor)
        return start, old_end, start + len(text), text

    return apply


def _mutate_delete(anchor: str):
    def apply(source: str):
        at = source.find(anchor)
        if at < 0:
            return None
        return at, at + len(anchor), at, ""

    return apply


# Ordered, deterministic. Each entry is applied at most once per step; the
# driver walks the list and takes the first that matches the current text, so a
# chain crosses every delimiter, quote, string-escape and nested-comment
# transition in a fixed order for a given base.
MUTATIONS = [
    ("delimiter/insert-rparen", _mutate_insert(")", ")", "after")),
    ("delimiter/insert-lbracket", _mutate_insert("[", "[", "after")),
    ("delimiter/insert-rbracket", _mutate_insert("]", "]", "after")),
    ("delimiter/insert-rbrace", _mutate_insert("}", "}", "after")),
    ("delimiter/insert-lparen", _mutate_insert("(", "(", "after")),
    # An opener with no closer is what drives the grammar into MISSING-node
    # recovery, so open one and take it back out again rather than hoping a
    # chain stumbles into it.
    ("delimiter/unbalance", _mutate_insert("(", "(", "before")),
    ("delimiter/rebalance", _mutate_delete("(")),
    ("quote/open", _mutate_insert("42", "'", "before")),
    ("quote/close", _mutate_insert("'", "'", "after")),
    ("quote/remove", _mutate_delete("'")),
    ("comment/nest-open", _mutate_insert("#|", " #|", "after")),
    ("comment/nest-close", _mutate_insert("|#", "|# ", "before")),
    ("comment/unterminate", _mutate_delete("|#")),
    ("comment/reterminate", _mutate_insert("#| c", "|#", "after")),
    ("string/escape", _mutate_insert('"', '\\"', "after")),
    ("string/remove-escape", _mutate_delete('\\"')),
    ("datum/nest", _mutate_insert("#;", " #;", "after")),
    ("datum/remove", _mutate_delete("#;")),
    ("whitespace/line", _mutate_insert("\n", "\n", "after")),
]


@dataclasses.dataclass
class Step:
    label: str
    path: Path
    start: int
    old_end: int
    new_end: int


def build_edit_chain(work: Path, label: str, base: str, steps: int) -> tuple:
    # Inside the run's own child directory: created, never pruned, and removed
    # with the run. Re-running one label rewrites the same step files, which is
    # why no staleness check is needed to spot leftover input.
    directory = work / "incremental" / label.replace("/", "_")
    directory.mkdir(parents=True, exist_ok=True)

    current = base
    files = [directory / f"{0:04d}.pt"]
    files[0].write_text(current, encoding="utf-8", errors="surrogateescape")
    edits = []
    applied = []
    index = 0
    misses = 0
    # Walk the mutation list in order, resuming after the one that fired, so a
    # chain crosses every transition instead of re-applying the first match.
    cursor = 0
    while index < steps and misses < len(MUTATIONS):
        progressed = False
        for offset in range(len(MUTATIONS)):
            name, apply_mutation = MUTATIONS[(cursor + offset) % len(MUTATIONS)]
            result = apply_mutation(current)
            if result is None:
                continue
            start, old_end, new_end, text = result
            nxt = current[:start] + text + current[old_end:]
            if nxt == current:
                continue
            current = nxt
            cursor = (cursor + offset + 1) % len(MUTATIONS)
            index += 1
            misses = 0
            path = directory / f"{index:04d}.pt"
            path.write_text(current, encoding="utf-8", errors="surrogateescape")
            files.append(path)
            edits.append((start, old_end, new_end))
            applied.append(Step(name, path, start, old_end, new_end))
            progressed = True
            break
        if not progressed:
            misses += 1
    return files, edits, applied


def compare_node_lists(incremental: list, fresh: list) -> tuple | None:
    """Return the first way a reused tree differs from a fresh one, or None.

    Every field in NODE_COMPARE_FIELDS must be PRESENT on both sides. A record
    missing one is a probe/dump bug, never agreement: reading it with `.get`
    would make two records that are both silent compare equal, which is the one
    outcome this comparison exists to rule out. So the fields are checked for
    existence first, named in the error when absent, and only then read
    directly.
    """
    for index, (left, right) in enumerate(zip(incremental, fresh)):
        for side, record in (("incremental", left), ("fresh", right)):
            missing = [field for field in NODE_COMPARE_FIELDS if field not in record]
            if missing:
                raise HarnessError(
                    f"{side} node {index} has no {', '.join(missing)} field "
                    f"(record keys: {sorted(record)}); the probe dump and "
                    "NODE_COMPARE_FIELDS disagree"
                )
        for field in NODE_COMPARE_FIELDS:
            if left[field] != right[field]:
                return (index, field, left[field], right[field], left, right)
    if len(incremental) != len(fresh):
        first_extra = min(len(incremental), len(fresh))
        longer, side = (
            (incremental, "incremental")
            if len(incremental) > len(fresh)
            else (fresh, "fresh")
        )
        return (
            first_extra,
            "length",
            len(incremental),
            len(fresh),
            longer[first_extra],
            {"side": side},
        )
    return None


def spans_of(records: list) -> list:
    """Sorted (type, start, end) triples for a side's opaque span records."""
    return sorted((r["type"], r["s"], r["e"]) for r in records)


def run_incremental(tools: Tools, work: Path, steps: int, verbose: bool) -> int:
    bases = list(INCREMENTAL_BASES)
    for name in ("src/prelude.pt", "demo/hello.pt"):
        candidate = tools.skript_root / name
        if candidate.is_file():
            bases.append(
                (
                    name.replace("/", "_"),
                    candidate.read_text(encoding="utf-8", errors="surrogateescape"),
                )
            )

    counts: dict = {}
    failures = []
    total_stages = 0
    for label, base in bases:
        files, edits, applied = build_edit_chain(work, label, base, steps)
        if not edits:
            counts["skipped_no_edits"] = counts.get("skipped_no_edits", 0) + 1
            continue
        stages, opaque = run_probe_incremental(tools, files, edits, verbose)
        for stage, sides in sorted(stages.items()):
            total_stages += 1
            diff = compare_node_lists(sides["incremental"], sides["fresh"])
            step = applied[stage]
            if diff is not None:
                index, field, left, right, left_rec, right_rec = diff
                counts[FAM_INCR] = counts.get(FAM_INCR, 0) + 1
                failures.append(
                    {
                        "base": label,
                        "mutation": step.label,
                        "stage": stage,
                        "edit": [step.start, step.old_end, step.new_end],
                        "node_index": index,
                        "field": field,
                        "incremental": left_rec,
                        "fresh": right_rec,
                    }
                )
                continue
            left_spans = spans_of(opaque.get(stage, {}).get("incremental", []))
            right_spans = spans_of(opaque.get(stage, {}).get("fresh", []))
            if left_spans != right_spans:
                counts[FAM_INCR] = counts.get(FAM_INCR, 0) + 1
                failures.append(
                    {
                        "base": label,
                        "mutation": step.label,
                        "stage": stage,
                        "edit": [step.start, step.old_end, step.new_end],
                        "node_index": "opaque",
                        "field": "opaque_span",
                        "incremental": left_spans,
                        "fresh": right_spans,
                    }
                )
                continue
            if verbose:
                print(
                    f"  ok {label} stage {stage} ({step.label}): "
                    f"{len(sides['incremental'])} nodes agree",
                    file=sys.stderr,
                )
    print(f"incremental: bases={len(bases)} stages={total_stages}")
    print("  divergences by family:")
    if not counts:
        print("    (none)")
    for family in sorted(counts):
        print(f"    {family}: {counts[family]}")
    shown = failures if verbose else failures[:MAX_REPORTED_CASES]
    for failure in shown:
        print(
            f"  FAIL {failure['base']} stage {failure['stage']} "
            f"mutation={failure['mutation']} edit={failure['edit']}"
        )
        print(
            f"    node {failure['node_index']} field {failure['field']}: "
            f"incremental={failure['incremental']!r} fresh={failure['fresh']!r}"
        )
    if len(shown) < len(failures):
        print(
            f"  ... {len(failures) - len(shown)} further stage(s) suppressed; "
            f"rerun with --verbose to list every one"
        )
    return 1 if counts.get(FAM_INCR) else 0


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def report(
    tools: Tools, mode: str, cases: list, counts: dict, failures: list, verbose: bool
):
    print(f"{mode}: cases={len(cases)}")
    print(
        f"  lexer: {tools.skript_root / 'src' / 'Token.zig'} sha256={tools.token_sha[:16]}…"
    )
    print(f"  parser: {SRC_DIR / 'parser.c'} + {tools.scanner.name}")
    print(f"  runtime: {tools.runtime.library} headers={tools.runtime.include_dir}")
    print("  divergences by family:")
    if not counts:
        print("    (none)")
    for family in sorted(counts):
        marker = "FATAL" if family in FATAL_FAMILIES else "ok   "
        print(f"    {marker} {family}: {counts[family]}")
    # One root cause can hit hundreds of generated cases, so the per-case
    # detail is capped; the family counts above always show the true totals.
    shown = failures if verbose else failures[:MAX_REPORTED_CASES]
    for case, src, oracle, view, opaque, divergences, fatal in shown:
        flag = "FAIL" if fatal else "note"
        print(f"  {flag} {case.name}: {len(src)} bytes")
        print(f"    source: {show_bytes(src)}")
        if oracle["status"] == "error":
            print(
                f"    oracle: {oracle['error']} at offset {oracle['offset']} "
                f"(line {oracle['line']} col {oracle['col']})"
            )
        else:
            print(f"    oracle: {len(oracle['tokens'])} tokens")
        print(
            f"    tree: {len(view.entries)} leaves, {len(view.comments)} comment nodes "
            f"excluded, {len(view.missing)} missing, {len(view.errors)} error, "
            f"{len(opaque)} opaque spans"
        )
        for div in divergences:
            print(f"    -> {div.family}")
            for key in sorted(div.detail):
                print(f"       {key}: {div.detail[key]}")
    if len(shown) < len(failures):
        print(
            f"  ... {len(failures) - len(shown)} further case(s) with the same "
            f"families suppressed; rerun with --verbose to list every one"
        )


def print_explanations() -> int:
    print("classified families (tolerated, each with required evidence):")
    for family, text in (
        (
            FAM_NUM,
            "reader UnexpectedToken where a num_lit ends and src[B] is not a terminator",
        ),
        (
            FAM_SHEBANG,
            "reader `#!` identifier whose exact range the tree dropped as a shebang",
        ),
        (
            FAM_CURLY,
            "difference inside one oracle-derived unbalanced-brace region with an ERROR/MISSING witness",
        ),
        (
            FAM_UNTERM,
            "reader UnexpectedToken on a `#|` that never closes, whose rest of "
            "input the tree kept as one block_comment",
        ),
    ):
        print(f"  {family}: {text}")
    print("fatal families (any occurrence fails the run):")
    for family in FATAL_FAMILIES:
        print(f"  {family}")
    print("informational (reported with evidence, never a waiver):")
    for family in INFORMATIONAL_FAMILIES:
        print(f"  {family}")
    return 0


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def run_mode(args, work: Path, verbose: bool) -> int:
    """Build both oracles inside the run's own directory and run one mode."""
    runtime = discover_runtime(args.runtime_include, args.runtime_library)
    skript_root = Path(args.skript).resolve()
    tools = build_tools(work, skript_root, runtime, verbose)
    if verbose:
        print(f"work dir: {work}", file=sys.stderr)
    if args.mode == "differential":
        cases = deterministic_cases()
        if not args.no_skript_files:
            for path in skript_sources(skript_root):
                cases.append(
                    Case(
                        f"skript/{path.relative_to(skript_root).as_posix()}",
                        path.read_text(encoding="utf-8", errors="surrogateescape"),
                        "skript",
                    )
                )
        if args.cases > 0:
            cases += generated_cases(args.cases, args.seed)
        return run_differential(tools, work, cases, verbose)
    return run_incremental(tools, work, args.steps, verbose)


def main(argv=None) -> int:
    # Shared options live in a parent parser used by BOTH the top-level parser
    # and each subcommand, every one of them with `default=SUPPRESS`. That way
    # `verify.py --work-dir X differential` and `verify.py differential
    # --work-dir X` parse identically, and a missing option never clobbers one
    # that was already given before the subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--explain",
        action="store_true",
        default=argparse.SUPPRESS,
        help="print the divergence families and exit",
    )
    common.add_argument(
        "--work-dir",
        default=argparse.SUPPRESS,
        help="parent directory for this run's private child, never written to "
        "directly; defaults to $TREE_SITTER_SKRIPT_VERIFY_WORKDIR, then the "
        "XDG cache, then /home/tmp2/omp/tree-sitter-skript-work. Contents are "
        "left untouched. Anything resolving inside /tmp or /var/tmp is rejected.",
    )
    common.add_argument(
        "--keep",
        action="store_true",
        default=argparse.SUPPRESS,
        help="keep this run's directory instead of removing it, and print its "
        "exact path",
    )
    common.add_argument(
        "--runtime-include",
        default=argparse.SUPPRESS,
        help="directory containing tree_sitter/api.h",
    )
    common.add_argument(
        "--runtime-library",
        default=argparse.SUPPRESS,
        help="path to libtree-sitter (this machine ships /usr/lib64/libtree-sitter.so.0.26)",
    )
    common.add_argument(
        "--skript",
        default=argparse.SUPPRESS,
        help="Skript checkout root (default: ../skript)",
    )
    common.add_argument(
        "--verbose",
        action="store_true",
        default=argparse.SUPPRESS,
        help="echo compile and run commands",
    )

    parser = argparse.ArgumentParser(
        prog="verify.py",
        parents=[common],
        description="Differential (lexer vs parser) and incremental verification.",
    )
    sub = parser.add_subparsers(dest="mode")

    diff = sub.add_parser(
        "differential",
        parents=[common],
        help="lex with Skript, parse with tree-sitter, compare leaves",
    )
    diff.add_argument(
        "--cases", type=int, default=2000, help="generated case count (0 disables)"
    )
    diff.add_argument("--seed", type=int, default=20261002, help="generator seed")
    diff.add_argument(
        "--no-skript-files", action="store_true", help="skip the live .pt corpus"
    )

    incr = sub.add_parser(
        "incremental",
        parents=[common],
        help="ts_tree_edit + reparse versus a fresh parse",
    )
    incr.add_argument("--steps", type=int, default=20, help="edits applied per base")

    args = parser.parse_args(argv)
    for name, default in (
        ("explain", False),
        ("work_dir", None),
        ("keep", False),
        ("runtime_include", None),
        ("runtime_library", None),
        ("skript", str(REPO_ROOT.parent / "skript")),
        ("verbose", False),
    ):
        if not hasattr(args, name):
            setattr(args, name, default)
    if args.explain:
        return print_explanations()
    if not args.mode:
        parser.print_help()
        return 2

    try:
        parent = resolve_work_dir(args.work_dir)
    except WorkDirError as exc:
        print(f"verify.py: {exc}", file=sys.stderr)
        return 2

    verbose = args.verbose
    try:
        # The parent is the caller's. The run happens in a child of it that
        # this process owns and removes on the way out, pass or fail.
        with owned_run_dir(parent, keep=args.keep) as work:
            return run_mode(args, work, verbose)
    except WorkDirError as exc:
        print(f"verify.py: {exc}", file=sys.stderr)
        return 2
    except HarnessError as exc:
        print(f"verify.py: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
