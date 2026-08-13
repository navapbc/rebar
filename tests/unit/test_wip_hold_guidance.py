"""A change with known issues is held in Gerrit WIP, not in a comment (ticket e30d-b5e7-f4de-4f91).

Change 1727 carried five known review defects. The hold was recorded in ticket comments and
a session log, and a peer session — correctly following the standing "both votes green =>
Submit" rule — submitted it anyway. The gap was mechanical, not disciplinary: nothing in the
contribution flow told an author how to make a hold *enforceable*, and Gerrit's own WIP state
(which blocks submit) went unmentioned everywhere.

So the rule has to be present on all three surfaces an author or agent actually reads, and
losing it from any one of them is the regression this module exists to catch. The guide
assertion deliberately goes through the packaged registry mapping rather than the file path:
reading the path directly would still pass if the guide stopped being packaged, and
``rebar explain review`` is the surface that actually has to carry the rule.
"""

from pathlib import Path

import pytest

from rebar.llm.plan_review import registry

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
CONTRIBUTING = ROOT / "CONTRIBUTING.md"
AGENTS = ROOT / "AGENTS.md"


def _contributing() -> str:
    return CONTRIBUTING.read_text(encoding="utf-8")


def _agents() -> str:
    return AGENTS.read_text(encoding="utf-8")


def _packaged_review_guide() -> str:
    """The guide body as ``rebar explain review`` resolves it, not as a file path."""
    body = registry.explain_guide("review")
    assert body, "the packaged code-review guide resolved empty"
    return body


# ── CONTRIBUTING.md is the authoritative wording ─────────────────────────────────


def test_contributing_gives_the_push_time_wip_form() -> None:
    assert "refs/for/main%wip" in _contributing()


def test_contributing_gives_the_rest_toggle_for_an_existing_change() -> None:
    """Which form applies depends on whether the change exists yet, so both must be there."""
    body = _contributing()
    assert "/wip" in body, "the REST endpoint for an already-pushed change is missing"
    assert "/ready" in body, "the unmark path is missing"


def test_contributing_states_why_a_ticket_comment_is_not_enough() -> None:
    """The rationale is the load-bearing part — without it the rule reads as ceremony."""
    body = _contributing()
    assert "advisory" in body
    assert "enforceable" in body
    assert "1727" in body, "the worked example that motivates the rule is missing"


# ── the packaged guide mirrors it (rebar explain review) ─────────────────────────


def test_the_packaged_review_guide_carries_the_rule() -> None:
    body = _packaged_review_guide()
    assert "Work In Progress" in body
    assert "%wip" in body


def test_the_packaged_review_guide_points_at_the_authoritative_wording() -> None:
    """Mirror, not a second source of truth."""
    assert "CONTRIBUTING.md" in _packaged_review_guide()


# ── AGENTS.md carries a pointer, not a restatement ───────────────────────────────


def test_agents_carries_the_pointer() -> None:
    body = _agents()
    assert "%wip" in body
    assert "[CONTRIBUTING.md](CONTRIBUTING.md) §2d" in body


def test_agents_does_not_restate_the_rationale_paragraph() -> None:
    """AGENTS.md's own rule is to point, not to duplicate; keep the pointer one bullet."""
    body = _agents()
    assert "Why the ticket comment isn't enough" not in body
