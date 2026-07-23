"""Plan-review dependency-staleness invalidation is consistent across sign paths (bug 790c).

A plan review is valid only while BOTH the subject's OWN plan material AND its dependencies'
(child/prerequisite) material are unchanged since the review ran — a change to either
INVALIDATES the review and requires a fresh `rebar review-plan`.

``generation.sign_manifest`` (the in-review sign) already enforces this: ``related_material``
is part of the immutable generation identity, so a dependency changing mid-review aborts
signing. But the sanctioned recovery ``resign_plan_review`` (``rebar sign-review``) only guarded
the subject's OWN material — its dependency-pin staleness check was gated behind
``verify.enforce_plan_material_pins`` (default ``False``) — so by default it re-certified a
review that no longer reflected a changed dependency. That is the bug: the two paths disagreed,
and ``sign-review`` could bypass a correct invalidation.

These pin the CONTRACT (observable ``review_plan`` / ``resign_plan_review`` / CLI output):

* dependency changed since the review  -> BOTH review-plan AND sign-review fail closed
  (sign-review must not re-certify it), and the message NAMES a dependency change.
* subject's OWN material changed         -> fails closed, message NAMES the own-material change.
* only UNRELATED store writes land       -> the PASS still certifies itself, no manual step
  (d70a scoped these out of generation identity).
* residual message: a genuine material change requires a fresh `rebar review-plan`; only a
  TRANSIENT (nothing-changed) failure points to the cheap `rebar sign-review <id>` recovery.
"""

from __future__ import annotations

import io
import re
import subprocess
from contextlib import redirect_stderr
from pathlib import Path

import pytest

import rebar
import rebar.llm
from rebar._cli import _llm_commands
from rebar.llm import findings as _f
from rebar.llm.plan_review import resign
from rebar.llm.runner import FakeRunner

_DESC = (
    "Body with enough length to be a real plan, describing the change in detail so the gate has "
    "something to review and the clarity heuristic is satisfied across the board here.\n\n"
    "## Acceptance Criteria\n- [ ] a thing is observably true\n- [ ] another verifiable check\n\n"
    "## Why\nx\n## What\ny\n## Scope\nz\n"
)


def _canned_verification(idx: int) -> dict:
    return {
        "finding_index": idx,
        "verdict": "upheld",
        "reason": "ok",
        "severity_attributes": {
            "prod_impact": "medium",
            "debt_impact": "medium",
            "blast_radius": "module",
            "likelihood": "medium",
            "reversibility": "moderate",
        },
        "binary": {
            "cited_reference_accurate": "na",
            "is_verifiable": "yes",
            "evidence_entails_finding": "yes",
            "path_reachable": "yes",
            "impact_follows_necessarily": "yes",
            "no_viable_alternative_explanation": "yes",
            "no_existing_mitigation": "yes",
            "severity_claim_justified": "yes",
        },
    }


class _GateFake(FakeRunner):
    """Offline schema-aware runner producing a clean PASS across finders/verify/coach."""

    name = "fake"

    def run(self, req) -> dict:  # type: ignore[override]
        schema = req.output_schema
        if req.mode == "text":
            return {"text": "[fake]", "runner": self.name, "model": None, "trace_id": None}
        if schema == "plan_review_findings":
            payload = {"analysis": "", "findings": []}
        elif schema == "plan_review_verification":
            idxs = [int(x) for x in re.findall(r"finding index (\d+)", req.instructions or "")]
            payload = {"verifications": [_canned_verification(i) for i in idxs]}
        elif schema == "plan_review_coach":
            payload = {"notes": []}
        else:
            payload = {"analysis": "", "findings": []}
        payload = _f.validate_structured(dict(payload), schema)
        return {**payload, "runner": self.name, "model": None, "trace_id": None}


class _MutatingFake(_GateFake):
    """A clean PASS runner that, on its FIRST finder call, commits a concurrent edit —
    deterministically landing a store write DURING the review, after ``initial_generation`` is
    captured but before the atomic sign."""

    def __init__(self, *, edit_ticket: str, repo: Path, new_desc: str) -> None:
        super().__init__()
        self._edit_ticket = edit_ticket
        self._repo = repo
        self._new_desc = new_desc
        self._fired = False

    def run(self, req) -> dict:  # type: ignore[override]
        if not self._fired and getattr(req, "output_schema", None) == "plan_review_findings":
            self._fired = True
            rebar.edit_ticket(
                self._edit_ticket, repo_root=str(self._repo), description=self._new_desc
            )
        return super().run(req)


def _epic_with_child(repo: Path) -> tuple[str, str]:
    epic = rebar.create_ticket("epic", "plan epic", description=_DESC, repo_root=str(repo))
    child = rebar.create_ticket(
        "task", "plan child", description=_DESC, repo_root=str(repo), parent=epic
    )
    subprocess.run(
        ["git", "commit", "--allow-empty", "-q", "-m", "init"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    return epic, child


def _review(tid: str, repo: Path, runner=None):
    return rebar.llm.review_plan(tid, runner=runner or _GateFake(), repo_root=str(repo))


# ── the core correctness bug: sign-review must NOT re-certify across a dependency change ──


def test_signreview_refuses_after_dependency_change(rebar_repo: Path) -> None:
    """CORE: a clean PASS is signed, then a CHILD's plan changes. `rebar sign-review` (the cheap
    recovery) must REFUSE — the recorded review no longer reflects the dependency. Previously it
    re-signed because pin enforcement defaults off."""
    epic, child = _epic_with_child(rebar_repo)
    res = _review(epic, rebar_repo)
    assert res["verdict"] == "PASS" and res["signature"]["signed"] is True, res["signature"]

    rebar.edit_ticket(child, repo_root=str(rebar_repo), description=_DESC + "\n- [ ] child moved\n")

    out = resign.resign_plan_review(epic, repo_root=str(rebar_repo))
    assert out["ok"] is False, out
    assert out["signed"] is False, out
    assert "depend" in str(out.get("reason", "")).lower(), out


def test_signreview_still_recovers_when_nothing_changed(rebar_repo: Path) -> None:
    """GUARD: sign-review must STILL recover a genuine transient — nothing materially changed —
    so the invalidation tightening does not break the legitimate cheap-recovery path."""
    epic, _child = _epic_with_child(rebar_repo)
    res = _review(epic, rebar_repo)
    assert res["signature"]["signed"] is True

    out = resign.resign_plan_review(epic, repo_root=str(rebar_repo))
    assert out["ok"] is True and out["signed"] is True, out


# ── review-plan fails closed on a material change, and NAMES what changed ──


def test_review_plan_dependency_change_fails_closed(rebar_repo: Path) -> None:
    """A CHILD changing mid-review aborts signing (correct invalidation); the message names a
    dependency change."""
    epic, child = _epic_with_child(rebar_repo)
    runner = _MutatingFake(
        edit_ticket=child, repo=rebar_repo, new_desc=_DESC + "\n- [ ] dep moved\n"
    )

    result = rebar.llm.review_plan(epic, runner=runner, repo_root=str(rebar_repo))

    assert result["verdict"] == "PASS"
    sig = result["signature"]
    assert sig["signed"] is False, sig
    assert "depend" in str(sig.get("error", "")).lower(), sig


def test_review_plan_own_material_change_names_the_change(rebar_repo: Path) -> None:
    """CRITERION 2: the SUBJECT's OWN material changing mid-review fails closed with a message that
    names the OWN-material change (not a generic 'generation changed')."""
    epic, _child = _epic_with_child(rebar_repo)
    runner = _MutatingFake(
        edit_ticket=epic, repo=rebar_repo, new_desc=_DESC + "\n- [ ] OWN plan rewritten\n"
    )

    result = rebar.llm.review_plan(epic, runner=runner, repo_root=str(rebar_repo))

    assert result["verdict"] == "PASS"
    sig = result["signature"]
    assert sig["signed"] is False, sig
    assert "own" in str(sig.get("error", "")).lower(), sig


def test_review_plan_unrelated_write_still_certifies(rebar_repo: Path) -> None:
    """CRITERION 1: an UNRELATED ticket's write landing mid-review does not invalidate — the PASS
    certifies itself with no manual step (d70a scoped store-wide state out of identity)."""
    epic, _child = _epic_with_child(rebar_repo)
    other = rebar.create_ticket("task", "unrelated", description=_DESC, repo_root=str(rebar_repo))
    runner = _MutatingFake(
        edit_ticket=other, repo=rebar_repo, new_desc=_DESC + "\n- [ ] unrelated moved\n"
    )

    result = rebar.llm.review_plan(epic, runner=runner, repo_root=str(rebar_repo))

    assert result["verdict"] == "PASS"
    assert result["signature"]["signed"] is True, result["signature"]


# ── the residual CLI message differentiates a material change from a transient ──


def test_cli_material_change_requires_re_review(capsys: pytest.CaptureFixture) -> None:
    """A material change (generation_changed) directs a fresh `rebar review-plan`; it must NOT
    point at sign-review (which now correctly refuses it)."""
    result = {
        "verdict": "PASS",
        "ticket_id": "790c-a7a3-120a-42c8",
        "signature": {
            "signed": False,
            "error": "a dependency's plan material changed since the review; re-review required",
            "event": "plan_review_generation_changed",
        },
        "coverage": {},
    }
    err = io.StringIO()
    with redirect_stderr(err):
        code = _llm_commands._disposition_exit_code(result, indeterminate_code=2)
    msg = err.getvalue().lower()
    assert code == 11, msg
    assert "review-plan" in msg, msg
    assert "sign-review" not in msg, msg


def test_cli_transient_points_to_sign_review(capsys: pytest.CaptureFixture) -> None:
    """A TRANSIENT failure (nothing materially changed) points to the cheap `rebar sign-review
    <id>` recovery — no LLM re-review."""
    result = {
        "verdict": "PASS",
        "ticket_id": "790c-a7a3-120a-42c8",
        "signature": {
            "signed": False,
            "error": "plan review generation remained unstable after 3 attempts",
            "event": "plan_review_generation_retry",
        },
        "coverage": {},
    }
    err = io.StringIO()
    with redirect_stderr(err):
        code = _llm_commands._disposition_exit_code(result, indeterminate_code=2)
    msg = err.getvalue()
    assert code == 11, msg
    assert "rebar sign-review 790c-a7a3-120a-42c8" in msg, msg
