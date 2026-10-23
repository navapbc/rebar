"""Root-cause flaky tests instead of retrying until green (9b79).

A leaked-logger flake caused four Verified -1 votes and repeated rechecks
before a class-wide fix. Both guidance files must state the rule and remove
the old license to recheck failures merely looking transient."""

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
AGENTS = ROOT / "AGENTS.md"
CONTRIBUTING = ROOT / "CONTRIBUTING.md"


def _agents() -> str:
    return AGENTS.read_text(encoding="utf-8")


def _contributing() -> str:
    return CONTRIBUTING.read_text(encoding="utf-8")


# ── the rule is present, with all three of its load-bearing parts ────────────────


def test_agents_states_a_flake_is_a_bug_not_a_retry() -> None:
    body = _agents()
    assert "A flaky test is a BUG to root-cause, never a retry." in body


def test_agents_names_the_deterministic_reproduction_expectation() -> None:
    """The rule has to say HOW to debug, or it is only a prohibition."""
    body = _agents()
    assert "/rebar-debug" in body, "the rule must point at the debugging workflow"
    assert "reproduce it deterministically" in body
    assert "fix the **class**" in body


def test_agents_carries_the_one_sentence_rationale() -> None:
    """All three costs the operator named, compressed to one sentence."""
    body = _agents()
    assert "slows development, wastes tokens, and erodes CI as a regression\n  oracle" in body, (
        "the rationale must name all three costs"
    )


def test_agents_carves_out_only_provably_environmental_faults() -> None:
    body = _agents()
    assert "`recheck` is reserved for provably environmental faults" in body
    assert "the recheck comment must state that reasoning" in body


# ── the old retry-until-green licence is gone from BOTH files ────────────────────


@pytest.mark.parametrize(
    ("name", "reader"),
    [("AGENTS.md", _agents), ("CONTRIBUTING.md", _contributing)],
)
def test_no_unqualified_recheck_for_a_merely_flaky_looking_failure(name: str, reader) -> None:
    """Neither guidance file may allow recheck for a failure that merely looks
    transient. The old wording licensed retrying an unfixed flaky test."""
    body = reader()
    assert "comment `recheck` if it is a flake" not in body, (
        f"{name} still licenses a bare recheck on a suspected flake"
    )
    assert "the failure looks transient/flaky" not in body, (
        f"{name} still treats 'looks flaky' as sufficient grounds to recheck"
    )


# ── CONTRIBUTING keeps the CI-triage home and names BOTH branches ────────────────


def test_contributing_splits_environmental_from_nondeterministic() -> None:
    """§6 stays the CI-triage home; it must route the two cases to opposite remedies."""
    body = _contributing()
    assert "**A provably environmental fault**" in body
    assert "**A nondeterministic test**" in body
    assert "`recheck` is not the remedy" in body
    assert "say in the comment why you believe it is environmental" in body


def test_contributing_cross_links_rather_than_restating() -> None:
    """One authoritative wording: CONTRIBUTING points at the AGENTS.md rule."""
    body = _contributing()
    assert "[AGENTS.md](AGENTS.md)" in body


def test_agents_points_back_at_the_triage_section() -> None:
    """The pointer is bidirectional, so neither file is a dead end."""
    assert "[CONTRIBUTING.md](CONTRIBUTING.md) §6" in _agents()
