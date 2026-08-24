"""Guard the packaged agent skills against ticket workflow regressions.

Ticket comely-craven-apatosaur establishes atomic claim behavior.

Ticket stealthful-calmy-erin establishes plan-review behavior.

Ticket helpful-stale-wildcat records the portable assignee rule.

Ticket arid-gutless-myna introduced the blocked-review wording that this module corrects.
"""

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
IMPLEMENT_SKILL = ROOT / "examples" / "agent-skills" / "rebar-implement" / "SKILL.md"
BRAINSTORM_SKILL = ROOT / "examples" / "agent-skills" / "rebar-brainstorm" / "SKILL.md"


def _implementation_guidance() -> str:
    return IMPLEMENT_SKILL.read_text(encoding="utf-8")


def _brainstorm_guidance() -> str:
    return BRAINSTORM_SKILL.read_text(encoding="utf-8")


def test_implementation_uses_atomic_claim_as_the_only_start_work_command() -> None:
    text = _implementation_guidance()
    assert "atomic `rebar claim <id>`" in text
    assert "`rebar claim <epic>`" in text
    assert "rebar transition <epic> open in_progress" not in text
    assert "rebar transition <id> open in_progress" not in text


def test_implementation_claim_uses_the_configured_default_identity() -> None:
    text = _implementation_guidance()
    assert "Run claim without `--assignee`" in text
    assert "`ticket.default_assignee`" in text
    assert "An empty setting leaves the ticket unassigned" in text
    assert "--assignee <you>" not in text


def test_implementation_limits_explicit_assignee_overrides() -> None:
    text = _implementation_guidance()
    assert "Pass `--assignee` only for an explicit override" in text
    assert "Jira-reconciled store" in text
    assert "email or accountId" in text
    assert "bare handle" in text


def test_brainstorm_marks_only_the_current_agent_action_complete() -> None:
    text = _brainstorm_guidance().lower()
    assert "current-cycle agent action is complete" in text
    assert "wait until the ticket becomes claimable" in text
    assert "completed the review cycle successfully" not in text


def test_brainstorm_defers_the_unsigned_indeterminate_verdict() -> None:
    text = _brainstorm_guidance()
    assert "unsigned `INDETERMINATE`" in text
    assert "`coverage.llm_ran` is `false`" in text
    assert "exit status is 2" in text
    assert "not a PASS" in text
    assert "passing attestation" in text
    assert "successful review verdict" in text


def test_both_skills_preserve_dependency_order_and_prohibit_agent_force() -> None:
    implementation = _implementation_guidance().lower()
    brainstorm = _brainstorm_guidance().lower()
    assert "dependency order" in implementation
    assert "never force" in implementation
    assert "in dependency order" in brainstorm
    assert "not an agent move" in brainstorm
