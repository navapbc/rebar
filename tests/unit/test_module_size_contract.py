"""Structural contract test for the module-size allowlist (epic drag-gripe-brake, Phase 4).

The module-size policy (docs/architecture.md) is enforced in CI by a shell one-liner in
``.github/workflows/test.yml`` (``find | wc -l | awk '$1 > 800' | comm -23 … allowlist``). That
shell check is BRITTLE in two ways the story set out to kill:

* it is text/stream plumbing (``awk``/``comm``/``grep``) with NO Python function a unit test can
  exercise, so a drift between the allowlist's format and the parser is invisible until CI; and
* ``comm -23`` only flags over-cap files MISSING from the allowlist — it never notices a STALE
  allowlist entry (a path that is no longer over cap, or is not a path at all). A literal
  ``</content>`` paste-artifact line sat in the allowlist undetected precisely because of this.

This test replaces that brittle, one-directional shell check with a STRUCTURAL assertion: it
computes the over-cap set from a function and asserts SET EQUALITY against the allowlist (both
directions) — no grep/glob/substring heuristics. A new over-cap file (missing from the allowlist)
OR a stale/garbage allowlist entry both fail it deterministically, with an actionable message.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "rebar"
ALLOWLIST = REPO_ROOT / ".github" / "module-size-allowlist.txt"
SOFT_CAP = 800  # docs/architecture.md "Module-size policy" — must match the CI gate's threshold.


def compute_over_cap_modules(src_root: Path, *, cap: int = SOFT_CAP) -> set[str]:
    """The repo-relative POSIX paths of ``src_root`` ``*.py`` files OVER ``cap`` lines.

    Line counting uses ``text.count("\\n")`` to match the CI gate's ``wc -l`` semantics exactly
    (``wc -l`` counts newlines), so this function and the shell gate can never disagree on which
    files are over cap. ``__pycache__`` is skipped, mirroring the gate's ``grep -v __pycache__``."""
    over: set[str] = set()
    for path in src_root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        line_count = path.read_text(encoding="utf-8", errors="surrogateescape").count("\n")
        if line_count > cap:
            over.add(path.relative_to(REPO_ROOT).as_posix())
    return over


def read_module_size_allowlist(path: Path) -> dict[str, int]:
    """The allowlist as ``{path: ceiling}`` (story S1 growth ratchet). Each non-blank,
    non-comment line is ``"<path> <max-lines>"``; a legacy bare-path line (no ceiling)
    parses to a ceiling of :data:`SOFT_CAP`. A dict, so the tests assert membership +
    the per-file ceiling structurally (not line order / text)."""
    out: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split()
        out[parts[0]] = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else SOFT_CAP
    return out


def test_allowlist_equals_computed_over_cap_set() -> None:
    """The module-size allowlist PATHS == the computed over-cap set (a STRUCTURAL contract,
    both directions). Catches a new over-cap offender (must be split or allowlisted) AND a
    stale or garbage allowlist entry (must be removed) — neither of which the one-directional
    shell ``comm -23`` gate detects."""
    over_cap = compute_over_cap_modules(SRC_ROOT)
    allowed = set(read_module_size_allowlist(ALLOWLIST))

    new_offenders = over_cap - allowed
    stale_entries = allowed - over_cap
    assert over_cap == allowed, (
        "module-size allowlist drifted from the computed over-cap set "
        f"(cap={SOFT_CAP} lines).\n"
        f"  NEW over-cap files NOT allow-listed (split them, or add to "
        f"{ALLOWLIST.name} + a docs/architecture.md remedy row): {sorted(new_offenders)}\n"
        f"  STALE allowlist entries no longer over cap / not a real over-cap path "
        f"(remove them, or lower/graduate the ceiling): {sorted(stale_entries)}"
    )


def test_allowlisted_files_within_pinned_ceilings() -> None:
    """The growth ratchet (story S1): every allow-listed file is AT OR UNDER its pinned
    ceiling. A grandfathered over-cap file can shrink freely but never grow past the number
    recorded in the allowlist — mirrors the CI ``Module-size gate`` shell step so a breach is
    caught locally too. (Growing a file requires deliberately raising its ceiling row.)"""
    breaches: list[str] = []
    for entry, ceiling in read_module_size_allowlist(ALLOWLIST).items():
        f = REPO_ROOT / entry
        if not f.is_file():
            continue  # covered by test_every_allowlist_entry_is_a_real_file
        loc = f.read_text(encoding="utf-8", errors="surrogateescape").count("\n")
        if loc > ceiling:
            breaches.append(f"{entry}: {loc} LOC > ceiling {ceiling}")
    assert not breaches, (
        "allow-listed file(s) exceeding their pinned ceiling (shrink them):\n  "
        + "\n  ".join(breaches)
    )


def test_every_allowlist_entry_is_a_real_file() -> None:
    """Every allowlist entry resolves to an existing repo file — a structural guard against a
    paste artifact or a renamed/deleted path lingering in the allowlist (e.g. the ``</content>``
    line this test was written to catch)."""
    missing = sorted(
        entry
        for entry in read_module_size_allowlist(ALLOWLIST)
        if not (REPO_ROOT / entry).is_file()
    )
    assert not missing, f"allowlist entries that are not real repo files: {missing}"


@pytest.mark.parametrize("cap", [SOFT_CAP])
def test_compute_over_cap_is_deterministic(cap: int) -> None:
    """The computation is a pure function of the tree (no ordering/IO nondeterminism): two calls
    return the same set."""
    assert compute_over_cap_modules(SRC_ROOT, cap=cap) == compute_over_cap_modules(
        SRC_ROOT, cap=cap
    )
