"""Structural contract test for the module-size allowlist (epic drag-gripe-brake, Phase 4).

The module-size policy (docs/architecture.md) is enforced in CI by a shell gate in
``.github/workflows/gerrit-verify.yaml`` (the gating ``Verified`` vote) and mirrored in
``.github/workflows/test.yml``. That shell check is BRITTLE in two ways the story set out to
kill:

* it is text/stream plumbing (``awk``/``comm``/``grep``) with NO Python function a unit test can
  exercise, so a drift between the files' format and the parser is invisible until CI; and
* ``comm -23`` only flags over-cap files MISSING from the allowlist — it never notices a STALE
  entry (a path that is no longer over cap, or is not a path at all). A literal ``</content>``
  paste-artifact line sat in the allowlist undetected precisely because of this.

The policy is expressed in TWO sibling files (story S1 growth ratchet):

* ``.github/module-size-allowlist.txt`` — soft-cap-exempt MEMBERSHIP, one bare path per line.
  Kept in the legacy bare-path format so the CI gate stays green in both directions across the
  ratchet rollout (a change adding the ratchet is verified by ``main``'s bare-path gate).
* ``.github/module-size-ceilings.txt`` — the per-file growth CEILING (``"<path> <max-lines>"``).

This test replaces the brittle, one-directional shell check with STRUCTURAL assertions: it
computes the over-cap set from a function and asserts SET EQUALITY against the membership file
(both directions), asserts the two files list the same paths, and asserts every file is within
its pinned ceiling — no grep/glob/substring heuristics.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "rebar"
ALLOWLIST = REPO_ROOT / ".github" / "module-size-allowlist.txt"
CEILINGS = REPO_ROOT / ".github" / "module-size-ceilings.txt"
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


def read_membership(path: Path) -> set[str]:
    """The soft-cap-exempt membership file as a set of repo-relative paths (bare-path format,
    first whitespace-delimited field of each non-blank, non-comment line)."""
    out: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        out.add(stripped.split()[0])
    return out


def read_ceilings(path: Path) -> dict[str, int]:
    """The ceilings file as ``{path: ceiling}`` (story S1 growth ratchet). Each non-blank,
    non-comment line is ``"<path> <max-lines>"``; a bare-path line (no integer ceiling) parses
    to a ceiling of :data:`SOFT_CAP`. A dict, so the tests assert membership + the per-file
    ceiling structurally (not line order / text)."""
    out: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split()
        out[parts[0]] = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else SOFT_CAP
    return out


def test_allowlist_equals_computed_over_cap_set() -> None:
    """The membership allowlist PATHS == the computed over-cap set (a STRUCTURAL contract,
    both directions). Catches a new over-cap offender (must be split or allowlisted) AND a
    stale or garbage allowlist entry (must be removed) — neither of which the one-directional
    shell ``comm -23`` gate detects."""
    over_cap = compute_over_cap_modules(SRC_ROOT)
    allowed = read_membership(ALLOWLIST)

    new_offenders = over_cap - allowed
    stale_entries = allowed - over_cap
    assert over_cap == allowed, (
        "module-size allowlist drifted from the computed over-cap set "
        f"(cap={SOFT_CAP} lines).\n"
        f"  NEW over-cap files NOT allow-listed (split them, or add to "
        f"{ALLOWLIST.name} + {CEILINGS.name} + a docs/architecture.md remedy row): "
        f"{sorted(new_offenders)}\n"
        f"  STALE allowlist entries no longer over cap / not a real over-cap path "
        f"(remove them, or lower/graduate the ceiling): {sorted(stale_entries)}"
    )


def test_membership_and_ceilings_list_the_same_paths() -> None:
    """The two sibling files are kept in sync: every membership path has a ceiling row and
    vice versa. A file exempt from the soft cap must carry a pinned ceiling, and a pinned
    ceiling must correspond to an exempt file — otherwise one of the two CI gate steps would
    silently disagree with the other."""
    membership = read_membership(ALLOWLIST)
    ceilings = set(read_ceilings(CEILINGS))
    missing_ceiling = membership - ceilings
    orphan_ceiling = ceilings - membership
    assert membership == ceilings, (
        "module-size membership and ceilings files are out of sync.\n"
        f"  MEMBERSHIP paths with no ceiling row in {CEILINGS.name}: {sorted(missing_ceiling)}\n"
        f"  CEILING rows with no membership line in {ALLOWLIST.name}: {sorted(orphan_ceiling)}"
    )


def test_allowlisted_files_within_pinned_ceilings() -> None:
    """The growth ratchet (story S1): every allow-listed file is AT OR UNDER its pinned
    ceiling. A grandfathered over-cap file can shrink freely but never grow past the number
    recorded in the ceilings file — mirrors the CI ``Module-size gate`` shell step so a breach
    is caught locally too. (Growing a file requires deliberately raising its ceiling row.)"""
    breaches: list[str] = []
    for entry, ceiling in read_ceilings(CEILINGS).items():
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
    """Every entry in BOTH files resolves to an existing repo file — a structural guard against
    a paste artifact or a renamed/deleted path lingering (e.g. the ``</content>`` line this test
    was written to catch)."""
    entries = read_membership(ALLOWLIST) | set(read_ceilings(CEILINGS))
    missing = sorted(entry for entry in entries if not (REPO_ROOT / entry).is_file())
    assert not missing, f"allowlist entries that are not real repo files: {missing}"


@pytest.mark.parametrize("cap", [SOFT_CAP])
def test_compute_over_cap_is_deterministic(cap: int) -> None:
    """The computation is a pure function of the tree (no ordering/IO nondeterminism): two calls
    return the same set."""
    assert compute_over_cap_modules(SRC_ROOT, cap=cap) == compute_over_cap_modules(
        SRC_ROOT, cap=cap
    )
