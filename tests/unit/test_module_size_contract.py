"""Enforce the absolute module-size cap without an allowlist (716f).

Read the limit from .github/module-size-limit.txt, the same source CI uses,
and count newlines like wc -l. Every src/rebar Python file must stay under it.
Normal contributions cannot change the locked limit."""

from __future__ import annotations

# The computation lives in ``tests/module_size_support`` so this gate mirror and the
# scc-backed size-metric reconciliation share ONE definition of the module-size rule
# (rebar-ticket c5b3-1b8a-08dd-40af). ``tests/`` is on sys.path by conftest guarantee.
from module_size_support import (
    LIMIT_FILE,
    REPO_ROOT,
    SRC_ROOT,
    compute_over_cap_modules,
    read_limit,
)

__all__ = ["LIMIT_FILE", "REPO_ROOT", "SRC_ROOT", "compute_over_cap_modules", "read_limit"]


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
    """The former bare-path allowlist and per-file ceiling file stay absent.

    The hard cap has no grandfathering escape hatch. Reintroducing those files
    would misrepresent CI behavior."""
    allowlist = REPO_ROOT / ".github" / "module-size-allowlist.txt"
    ceilings = REPO_ROOT / ".github" / "module-size-ceilings.txt"
    assert not allowlist.exists(), (
        f"{allowlist} should not exist — the allowlist escape hatch was removed (epic 716f)"
    )
    assert not ceilings.exists(), (
        f"{ceilings} should not exist — the ceilings ratchet was removed (epic 716f)"
    )
