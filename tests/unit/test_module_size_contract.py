"""Structural contract for the module-size hard cap (epic 716f, Phase 2).

The module-size policy (docs/architecture.md) used to be a *growth ratchet* with a
grandfathering escape hatch — a bare-path allowlist plus a per-file ceilings file. Epic 716f
split every over-cap module and drained that allowlist to zero, then removed the escape hatch
entirely: the cap is now ABSOLUTE. No ``src/rebar`` module may exceed the limit, and there is
no allowlist to exempt one.

The limit itself is single-sourced in ``.github/module-size-limit.txt`` — the SAME file the CI
``Module-size gate`` reads — so the gate and this test can never disagree on the number. That
file is LOCKED by the CI gate: changing the value requires an administrator to override the
gate (force-submit); a normal contributor change to it fails ``Verified``.

This test mirrors the gate in-process: it reads the limit from the single source and asserts
no ``src/rebar`` ``*.py`` file exceeds it. Line counting uses ``text.count("\\n")`` to match the
gate's ``wc -l`` semantics exactly (``wc -l`` counts newlines).
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "rebar"
LIMIT_FILE = REPO_ROOT / ".github" / "module-size-limit.txt"


def read_limit() -> int:
    """The module-size limit, read from the single source the CI gate also reads."""
    return int(LIMIT_FILE.read_text(encoding="utf-8").strip())


def compute_over_cap_modules(src_root: Path, *, cap: int) -> dict[str, int]:
    """Repo-relative POSIX path -> LOC for every ``src_root`` ``*.py`` file OVER ``cap`` lines.

    Line counting uses ``text.count("\\n")`` to match the CI gate's ``wc -l`` semantics exactly.
    ``__pycache__`` is skipped, mirroring the gate's ``grep -v __pycache__``."""
    over: dict[str, int] = {}
    for path in src_root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        loc = path.read_text(encoding="utf-8", errors="surrogateescape").count("\n")
        if loc > cap:
            over[path.relative_to(REPO_ROOT).as_posix()] = loc
    return over


def test_limit_file_is_a_single_positive_int() -> None:
    """The limit is single-sourced in ``.github/module-size-limit.txt`` as one positive int —
    the same file the CI ``Module-size gate`` reads (and locks)."""
    assert LIMIT_FILE.is_file(), f"{LIMIT_FILE} (the single-source module-size limit) is missing"
    limit = read_limit()
    assert limit > 0, f"module-size limit must be a positive integer, got {limit}"


def test_no_src_module_over_the_hard_cap() -> None:
    """The cap is ABSOLUTE: no ``src/rebar`` ``*.py`` file may exceed the limit. There is no
    allowlist escape hatch (epic 716f removed it) — a new over-cap file must be split."""
    cap = read_limit()
    over = compute_over_cap_modules(SRC_ROOT, cap=cap)
    assert over == {}, (
        f"src/rebar file(s) over the {cap}-LOC hard cap (split them along a real call-graph "
        f"seam — there is no allowlist to exempt them): {dict(sorted(over.items()))}"
    )


def test_compute_over_cap_is_deterministic() -> None:
    """The computation is a pure function of the tree (no ordering/IO nondeterminism)."""
    cap = read_limit()
    assert compute_over_cap_modules(SRC_ROOT, cap=cap) == compute_over_cap_modules(
        SRC_ROOT, cap=cap
    )


def test_allowlist_mechanism_removed() -> None:
    """The grandfathering escape hatch is gone for good (epic 716f): neither the bare-path
    allowlist nor the per-file ceilings file exists any more. The cap is absolute; a re-added
    allowlist would be dead (the CI gate no longer reads it), so this guards against quietly
    resurrecting the escape hatch."""
    allowlist = REPO_ROOT / ".github" / "module-size-allowlist.txt"
    ceilings = REPO_ROOT / ".github" / "module-size-ceilings.txt"
    assert not allowlist.exists(), (
        f"{allowlist} should not exist — the allowlist escape hatch was removed (epic 716f)"
    )
    assert not ceilings.exists(), (
        f"{ceilings} should not exist — the ceilings ratchet was removed (epic 716f)"
    )
