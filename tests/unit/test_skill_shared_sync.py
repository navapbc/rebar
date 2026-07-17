"""Drift gate for the shared skill test-design standard (task 43c9).

``examples/agent-skills/shared/test-design.md`` is the canonical copy; each
consuming skill ships a byte-identical real-file copy so a skill directory is
self-contained in every load context (checkout, symlinked skills dir, plain
copy). This gate fails when a copy diverges or goes missing — sync with:

    for s in rebar-debug rebar-implement rebar-brainstorm; do
        cp examples/agent-skills/shared/test-design.md \
           examples/agent-skills/$s/test-design.md
    done

The consumer set is deliberately hardcoded: adding a consumer means adding it
here and to the sync one-liner together (rebar-janitor is not a consumer — it
authors no tests).
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILLS_DIR = REPO_ROOT / "examples" / "agent-skills"
CANONICAL = SKILLS_DIR / "shared" / "test-design.md"
CONSUMERS = ("rebar-debug", "rebar-implement", "rebar-brainstorm")


def _identical(canonical: Path, copy: Path) -> bool:
    """True iff *copy* exists and is byte-identical to *canonical*."""
    return copy.is_file() and copy.read_bytes() == canonical.read_bytes()


def test_canonical_file_exists() -> None:
    assert CANONICAL.is_file(), f"canonical shared standard missing: {CANONICAL}"


@pytest.mark.parametrize("skill", CONSUMERS)
def test_skill_copy_is_byte_identical(skill: str) -> None:
    copy = SKILLS_DIR / skill / "test-design.md"
    assert _identical(CANONICAL, copy), (
        f"{copy} is missing or diverges from {CANONICAL}; re-sync with the "
        "cp one-liner in this module's docstring"
    )


def test_divergence_is_detected(tmp_path: Path) -> None:
    """The comparison this gate relies on detects a perturbed copy."""
    perturbed = tmp_path / "test-design.md"
    perturbed.write_bytes(CANONICAL.read_bytes() + b"\n<!-- drift -->\n")
    assert not _identical(CANONICAL, perturbed)
    missing = tmp_path / "missing.md"
    assert not _identical(CANONICAL, missing)
