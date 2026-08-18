"""Claim-gate coverage for the plan-review gate (epic 5fd2).

The gate (rebar._commands.gates.plan_review_precheck, wired into BOTH claim_compute and
transition_compute's open->in_progress arm) is opt-in via
``verify.require_plan_review_for_claim``. Unlike the completion CLOSE gate (which
runs the LLM at close time), the CLAIM gate is a FAST, LOCAL signature check — no LLM, no
network — and the heavy three-pass review runs OUT-OF-BAND via ``review_plan`` (driven here with
a FakeRunner, so still no model/network). These tests assert the deterministic behavior:

  * gate OFF (default) → claim without any attestation (today's behavior);
  * gate ON + no attestation → claim BLOCKED (exit 1), ticket stays open;
  * review_plan (clean) signs an attestation → claim then SUCCEEDS;
  * gate ON + --force → claim succeeds, an audit comment records the bypass;
  * a material edit after review INVALIDATES the attestation → claim blocked again;
  * bugs / session_logs are EXEMPT (claim succeeds with no attestation);
  * the DET floor blocks review_plan (no AC) → no signature → claim still blocked;
  * the REVIEW_RESULT sidecar is reducer-IGNORED (status intact; fsck recognises it);
  * the claim path makes NO LLM / NO network call (a pure local HMAC verify).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

import rebar
import rebar.llm
from rebar import _cli
from rebar import config as _config
from rebar.llm.runner import FakeRunner


def _canned_verification(index: int) -> dict:
    """A high-validity verification (so a finder finding SURVIVES Pass-3 as an advisory)."""
    return {
        "index": index,
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
    """A schema-aware OFFLINE runner that returns shape-valid output for each plan-review
    pass (finders → verify → coach), so ``review_plan`` runs to a real verdict on the
    workflow engine. The bespoke fixed-payload ``FakeRunner`` could not satisfy the
    verify/coach schemas (B-RETIRE made the workflow the sole gate); this small fake does.

    ``finder_error`` (a per-finder hiccup) raises ONLY on finder calls, exercising the
    tier-ran fail-open path while verify/coach still resolve."""

    name = "fake"

    def __init__(self, *, finder_findings=None, finder_error=None):
        super().__init__()
        self._finder_findings = finder_findings or []
        self._finder_error = finder_error

    def run(self, req) -> dict:  # type: ignore[override]
        from rebar.llm import findings as _f

        schema = req.output_schema
        if req.mode == "text":
            return {"text": "[fake summary]", "runner": self.name, "model": None, "trace_id": None}
        if schema == "plan_review_findings":
            if self._finder_error is not None:
                raise self._finder_error
            payload = {"analysis": "", "findings": list(self._finder_findings)}
        elif schema == "plan_review_verification":
            idxs = [int(x) for x in re.findall(r"finding index (\d+)", req.instructions or "")]
            payload = {"verifications": [_canned_verification(i) for i in idxs]}
        elif schema == "plan_review_coach":
            payload = {"notes": []}
        else:
            payload = {"analysis": "", "findings": []}
        payload = _f.validate_structured(dict(payload), schema)
        return {**payload, "runner": self.name, "model": None, "trace_id": None}


_CLEAN = _GateFake()

_DESC = (
    "Body with enough length to be a real plan, describing the change in detail so the gate has "
    "something to review and the clarity heuristic is satisfied across the board here.\n\n"
    "## Acceptance Criteria\n- [ ] a thing is observably true\n- [ ] another verifiable check\n\n"
    "## Why\nx\n## What\ny\n## Scope\nz\n## Testing\nRun `pytest -q`.\n"
)


def _enable(repo: Path) -> None:
    # progressive drift-refresh is always on (off switch retired in story 4cdf); the gate config
    # no longer sets it.
    (repo / "rebar.toml").write_text("[verify]\nrequire_plan_review_for_claim = true\n")


def _commit(repo: Path) -> None:
    subprocess.run(
        ["git", "commit", "--allow-empty", "-q", "-m", "c"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )


def _commit_all(repo: Path) -> None:
    """STAGE the working tree, then commit — as opposed to :func:`_commit`, which makes an
    empty commit purely to advance HEAD.

    The gates read an ATTESTED snapshot of a committed tree (see the suite-wide default in
    ``tests/conftest.py``), so file content only exists for a gate once it is committed.
    Writing a file and calling ``_commit`` would advance HEAD while leaving the content
    invisible — which is how the drift tests silently passed against the live checkout."""
    subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-q", "-m", "c"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )


def _make(repo: Path, ttype: str = "task", desc: str = _DESC) -> str:
    return rebar.create_ticket(ttype, f"plan {ttype}", description=desc, repo_root=str(repo))


def _status(tid: str, repo: Path) -> str:
    return rebar.show_ticket(tid, repo_root=str(repo))["status"]


def _review(tid: str, repo: Path, runner=_CLEAN):
    return rebar.llm.review_plan(tid, runner=runner, repo_root=str(repo))


# ── gate off (default) ─────────────────────────────────────────────────────────
def test_gate_off_by_default_claims_without_attestation(rebar_repo: Path) -> None:
    tid = _make(rebar_repo)
    rebar.claim(tid, assignee="me", repo_root=str(rebar_repo))
    assert _status(tid, rebar_repo) == "in_progress"


# ── gate on, missing attestation blocks ─────────────────────────────────────────
def test_gate_on_blocks_claim_without_attestation(rebar_repo: Path) -> None:
    _commit(rebar_repo)
    _enable(rebar_repo)
    tid = _make(rebar_repo)
    with pytest.raises(rebar.RebarError) as ei:
        rebar.claim(tid, repo_root=str(rebar_repo))
    assert ei.value.returncode == 1
    assert "review-plan" in ei.value.stderr  # the recovery hint names the remedy
    assert _status(tid, rebar_repo) == "open"  # never claimed


# ── earn an attestation → claim succeeds (the full loop) ───────────────────────
def test_review_then_claim_succeeds(rebar_repo: Path) -> None:
    _commit(rebar_repo)
    _enable(rebar_repo)
    tid = _make(rebar_repo)
    verdict = _review(tid, rebar_repo)
    assert verdict["verdict"] == "PASS" and verdict["signature"]["signed"]
    rebar.claim(tid, assignee="me", repo_root=str(rebar_repo))
    assert _status(tid, rebar_repo) == "in_progress"


# ── --force bypass + audit ─────────────────────────────────────────────────────
def test_force_bypasses_gate_with_audit(rebar_repo: Path) -> None:
    _commit(rebar_repo)
    _enable(rebar_repo)
    tid = _make(rebar_repo)
    rebar.claim(tid, force="urgent hotfix", repo_root=str(rebar_repo))
    assert _status(tid, rebar_repo) == "in_progress"
    comments = " ".join(
        c.get("body", "")
        for c in rebar.show_ticket(tid, repo_root=str(rebar_repo)).get("comments", [])
    )
    assert "FORCE_CLAIM" in comments and "urgent hotfix" in comments


# ── transition open->in_progress goes through the SAME gate as claim ────────────
def test_transition_to_in_progress_blocked_without_attestation(rebar_repo: Path) -> None:
    # Starting work via a plain `transition open in_progress` is gated identically to
    # claim: with the gate on and no attestation, it is BLOCKED and the ticket stays open.
    _commit(rebar_repo)
    _enable(rebar_repo)
    tid = _make(rebar_repo)
    with pytest.raises(rebar.RebarError) as ei:
        rebar.transition(tid, "open", "in_progress", repo_root=str(rebar_repo))
    assert ei.value.returncode == 1
    assert "review-plan" in ei.value.stderr  # same recovery hint as the claim path
    assert _status(tid, rebar_repo) == "open"  # never started


def test_transition_to_in_progress_succeeds_after_review(rebar_repo: Path) -> None:
    _commit(rebar_repo)
    _enable(rebar_repo)
    tid = _make(rebar_repo)
    assert _review(tid, rebar_repo)["signature"]["signed"]
    rebar.transition(tid, "open", "in_progress", repo_root=str(rebar_repo))
    assert _status(tid, rebar_repo) == "in_progress"


def test_transition_to_in_progress_force_bypasses_with_audit(rebar_repo: Path) -> None:
    _commit(rebar_repo)
    _enable(rebar_repo)
    tid = _make(rebar_repo)
    # The force-bypass reason is carried by `force` itself (unified surface, ticket
    # blusterous-earthly-kitten); `reason` no longer doubles as the force note.
    rebar.transition(tid, "open", "in_progress", force="urgent hotfix", repo_root=str(rebar_repo))
    assert _status(tid, rebar_repo) == "in_progress"
    comments = " ".join(
        c.get("body", "")
        for c in rebar.show_ticket(tid, repo_root=str(rebar_repo)).get("comments", [])
    )
    assert "FORCE_CLAIM" in comments and "urgent hotfix" in comments


def test_transition_to_in_progress_bug_is_exempt(rebar_repo: Path) -> None:
    _commit(rebar_repo)
    _enable(rebar_repo)
    bug_desc = (
        "A real bug body of sufficient length.\n\n## Reproduction Steps\n1. do x\n\n"
        "Expected: a; Actual: b\n\n## Acceptance Criteria\n- [ ] fixed (checked: `pytest -q`)\n"
    )
    tid = _make(rebar_repo, "bug", desc=bug_desc)
    rebar.transition(tid, "open", "in_progress", repo_root=str(rebar_repo))  # exempt
    assert _status(tid, rebar_repo) == "in_progress"


def test_transition_to_in_progress_stale_attestation_blocks(rebar_repo: Path) -> None:
    # Staleness (an unscoped attestation invalidated by a later code commit) blocks the
    # transition start-work edge exactly as it blocks claim.
    _commit(rebar_repo)
    _enable(rebar_repo)
    tid = _make(rebar_repo)
    assert _review(tid, rebar_repo)["signature"]["signed"]
    _commit(rebar_repo)  # HEAD advances; nothing scoped → whole-HEAD freshness fails
    with pytest.raises(rebar.RebarError) as ei:
        rebar.transition(tid, "open", "in_progress", repo_root=str(rebar_repo))
    assert "stale" in ei.value.stderr.lower()
    assert _status(tid, rebar_repo) == "open"


def test_import_preserves_in_progress_under_gate(rebar_repo: Path) -> None:
    # An NDJSON import re-materializes an already-existing in_progress status via
    # transition_compute with cascade=False (replay). The new start-work gate must NOT
    # block that replay (it is gated on `cascade`) — the imported ticket lands in_progress.
    _commit(rebar_repo)
    _enable(rebar_repo)
    record = {
        "ticket_id": "src-in-progress-0001",  # source id; idempotency key
        "ticket_type": "task",
        "title": "imported in-progress task",
        "description": _DESC,
        "status": "in_progress",
    }
    res = rebar.import_tickets([record], repo_root=str(rebar_repo))
    assert res["created"] == 1
    imported = [
        t for t in rebar.list_tickets(repo_root=str(rebar_repo)) if t["status"] == "in_progress"
    ]
    assert imported, "imported in_progress ticket was downgraded to open by the gate"


def test_transition_gate_off_by_default(rebar_repo: Path) -> None:
    # With the gate off (default), transition open->in_progress needs no attestation.
    tid = _make(rebar_repo)
    rebar.transition(tid, "open", "in_progress", repo_root=str(rebar_repo))
    assert _status(tid, rebar_repo) == "in_progress"


def test_non_startwork_transition_is_not_gated(rebar_repo: Path) -> None:
    # The gate guards only the open->in_progress start-work edge: closing an already
    # in_progress ticket (force-started here) is unaffected by the plan-review gate.
    _commit(rebar_repo)
    _enable(rebar_repo)
    tid = _make(rebar_repo)
    rebar.transition(
        tid, "open", "in_progress", force="(no reason given)", repo_root=str(rebar_repo)
    )
    # Closing is unaffected by the plan-review (start-work) gate; the completion-close
    # gate is not enabled in this repo, so a plain close succeeds.
    rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))
    assert _status(tid, rebar_repo) == "closed"


# ── gate × parent-first cascade (the #47 cascade is preserved under the gate) ────
def test_transition_cascade_gates_child_before_reaching_parent(rebar_repo: Path) -> None:
    # The child's OWN gate fires FIRST — before the cascade reaches the parent. With the
    # gate on and the child un-reviewed, the block is the child's own gate error (it names
    # the child, NOT the parent), proving the parent cascade was never attempted; neither
    # ticket moves.
    _commit(rebar_repo)
    _enable(rebar_repo)
    parent = rebar.create_ticket(
        "epic", "parent epic", description=_DESC, repo_root=str(rebar_repo)
    )
    child = rebar.create_ticket(
        "task", "child task", description=_DESC, parent=parent, repo_root=str(rebar_repo)
    )
    with pytest.raises(rebar.RebarError) as ei:
        rebar.transition(child, "open", "in_progress", repo_root=str(rebar_repo))
    assert f"cannot start work on {child}" in ei.value.stderr  # the CHILD's own gate
    assert parent not in ei.value.stderr  # cascade never reached the parent
    assert _status(child, rebar_repo) == "open"
    assert _status(parent, rebar_repo) == "open"  # cascade never ran; parent untouched


def test_transition_cascade_parent_gate_blocks_and_names_parent(rebar_repo: Path) -> None:
    # The gate applies to the cascaded PARENT too: a child with a valid attestation whose
    # parent is un-reviewed is blocked by the PARENT's gate during the cascade; the error
    # names the parent and neither ticket moves.
    _commit(rebar_repo)
    _enable(rebar_repo)
    parent = rebar.create_ticket(
        "epic", "parent epic", description=_DESC, repo_root=str(rebar_repo)
    )
    child = rebar.create_ticket(
        "task", "child task", description=_DESC, parent=parent, repo_root=str(rebar_repo)
    )
    assert _review(child, rebar_repo)["signature"]["signed"]  # only the child is reviewed
    with pytest.raises(rebar.RebarError) as ei:
        rebar.transition(child, "open", "in_progress", repo_root=str(rebar_repo))
    assert parent in ei.value.stderr  # the cascade hit the parent's gate, named the parent
    assert _status(child, rebar_repo) == "open"  # child not moved when its parent is blocked
    assert _status(parent, rebar_repo) == "open"


def test_transition_cascade_force_propagates_up_the_chain(rebar_repo: Path) -> None:
    # The cascade is preserved AND the --force bypass propagates up it: force-starting a
    # child whose parent is also un-reviewed moves BOTH to in_progress (claim/transition
    # parity — claim already propagates its force up the chain).
    _commit(rebar_repo)
    _enable(rebar_repo)
    parent = rebar.create_ticket(
        "epic", "parent epic", description=_DESC, repo_root=str(rebar_repo)
    )
    child = rebar.create_ticket(
        "task", "child task", description=_DESC, parent=parent, repo_root=str(rebar_repo)
    )
    rebar.transition(child, "open", "in_progress", force="urgent", repo_root=str(rebar_repo))
    assert _status(child, rebar_repo) == "in_progress"
    assert _status(parent, rebar_repo) == "in_progress"  # cascade preserved + force propagated


# ── claim ≡ transition parity (one shared gate, behaviorally identical) ──────────
@pytest.mark.parametrize("start", ["claim", "transition"])
def test_claim_and_transition_enforce_the_same_gate(rebar_repo: Path, start: str) -> None:
    # claim and transition open->in_progress are consolidated onto ONE gate method, so they
    # MUST behave identically: same block-without-attestation, same recovery hint, same
    # success after earning the attestation. Running the full sequence through each entry
    # point guards against the two paths silently diverging again.
    _commit(rebar_repo)
    _enable(rebar_repo)
    tid = _make(rebar_repo)

    def _start() -> None:
        if start == "claim":
            rebar.claim(tid, repo_root=str(rebar_repo))
        else:
            rebar.transition(tid, "open", "in_progress", repo_root=str(rebar_repo))

    with pytest.raises(rebar.RebarError) as ei:
        _start()
    assert ei.value.returncode == 1
    assert "review-plan" in ei.value.stderr
    assert _status(tid, rebar_repo) == "open"

    assert _review(tid, rebar_repo)["signature"]["signed"]
    _start()
    assert _status(tid, rebar_repo) == "in_progress"


# ── CLI end-to-end (the start-work gate over the real CLI entry point) ───────────
def test_transition_cli_start_work_gate_blocks_exit_1(rebar_repo: Path, capsys) -> None:
    # The CLI `transition` maps a gate block to exit 1 and prints the review-plan recovery
    # hint to stderr; the ticket stays open (no mutation).
    _commit(rebar_repo)
    _enable(rebar_repo)
    tid = _make(rebar_repo)
    rc = _cli.main(["transition", tid, "open", "in_progress"])
    assert rc == 1
    assert "review-plan" in capsys.readouterr().err
    assert _status(tid, rebar_repo) == "open"


def test_transition_cli_force_reason_bypasses_exit_0_with_audit(rebar_repo: Path) -> None:
    # Over the CLI, `--force=<reason>` bypasses the gate (exit 0) and the reason becomes the
    # FORCE_CLAIM audit note — the full flag→force_reason conversion exercised end-to-end.
    # `--reason` no longer feeds the force note (ticket blusterous-earthly-kitten).
    _commit(rebar_repo)
    _enable(rebar_repo)
    tid = _make(rebar_repo)
    rc = _cli.main(["transition", tid, "open", "in_progress", "--force=urgent cli"])
    assert rc == 0
    assert _status(tid, rebar_repo) == "in_progress"
    comments = " ".join(
        c.get("body", "")
        for c in rebar.show_ticket(tid, repo_root=str(rebar_repo)).get("comments", [])
    )
    assert "FORCE_CLAIM" in comments and "urgent cli" in comments


# ── invalidation + lifecycle boundaries on the transition edge ───────────────────
def test_transition_material_edit_invalidates_attestation(rebar_repo: Path) -> None:
    # The transition edge honors material-edit invalidation exactly like claim: editing the
    # plan's description after review invalidates the attestation and re-blocks the start.
    _commit(rebar_repo)
    _enable(rebar_repo)
    tid = _make(rebar_repo)
    _review(tid, rebar_repo)
    rebar.edit_ticket(
        tid,
        description=_DESC + "\nNEW materially-different requirement.",
        repo_root=str(rebar_repo),
    )
    with pytest.raises(rebar.RebarError) as ei:
        rebar.transition(tid, "open", "in_progress", repo_root=str(rebar_repo))
    assert "materially edited" in ei.value.stderr
    assert _status(tid, rebar_repo) == "open"


@pytest.mark.parametrize("intermediate", ["blocked", "closed"])
def test_side_door_into_in_progress_is_gated(rebar_repo: Path, intermediate: str) -> None:
    # NO alternate edge into in_progress can start un-reviewed work past the gate: routing
    # through `blocked` OR `closed` first and then into in_progress is gated exactly like a
    # direct open->in_progress start. Keying the gate on the TARGET (not current=="open")
    # closes every side-door — both are reachable over MCP via plain transition calls, with
    # no force needed, so an `open->{blocked|closed}->in_progress` bypass would otherwise
    # let an agent start un-reviewed work.
    _commit(rebar_repo)
    _enable(rebar_repo)
    tid = _make(rebar_repo)
    rebar.transition(
        tid, "open", intermediate, repo_root=str(rebar_repo)
    )  # ungated (target≠in_prog)
    with pytest.raises(rebar.RebarError) as ei:
        rebar.transition(tid, intermediate, "in_progress", repo_root=str(rebar_repo))
    assert "review-plan" in ei.value.stderr
    assert _status(tid, rebar_repo) == intermediate  # not started


def test_resume_from_blocked_passes_with_valid_attestation(rebar_repo: Path) -> None:
    # Entering in_progress is gated even from `blocked`, but a legitimately-reviewed ticket
    # keeps a VALID attestation, so the normal in_progress->blocked->in_progress resume
    # cycle passes cleanly (no material edit / code drift ⇒ the signature still certifies).
    _commit(rebar_repo)
    _enable(rebar_repo)
    tid = _make(rebar_repo)
    assert _review(tid, rebar_repo)["signature"]["signed"]
    rebar.transition(tid, "open", "in_progress", repo_root=str(rebar_repo))  # gated; attest valid
    rebar.transition(tid, "in_progress", "blocked", repo_root=str(rebar_repo))
    rebar.transition(
        tid, "blocked", "in_progress", repo_root=str(rebar_repo)
    )  # resume; still valid
    assert _status(tid, rebar_repo) == "in_progress"


# ── material-edit invalidation ─────────────────────────────────────────────────
def test_material_edit_invalidates_attestation(rebar_repo: Path) -> None:
    _commit(rebar_repo)
    _enable(rebar_repo)
    tid = _make(rebar_repo)
    _review(tid, rebar_repo)
    # Edit the plan's MATERIAL content (description) — no code commit, so HEAD is unchanged.
    rebar.edit_ticket(
        tid,
        description=_DESC + "\nNEW materially-different requirement.",
        repo_root=str(rebar_repo),
    )
    with pytest.raises(rebar.RebarError) as ei:
        rebar.claim(tid, repo_root=str(rebar_repo))
    assert "materially edited" in ei.value.stderr
    assert _status(tid, rebar_repo) == "open"


# ── exemptions ──────────────────────────────────────────────────────────────────
def test_bug_is_exempt_from_claim_gate(rebar_repo: Path) -> None:
    _commit(rebar_repo)
    _enable(rebar_repo)
    bug_desc = (
        "A real bug body of sufficient length.\n\n## Reproduction Steps\n1. do x\n\n"
        "Expected: a; Actual: b\n\n## Acceptance Criteria\n- [ ] fixed (checked: `pytest -q`)\n"
    )
    tid = _make(rebar_repo, "bug", desc=bug_desc)
    rebar.claim(tid, repo_root=str(rebar_repo))  # no attestation needed — bugs are exempt
    assert _status(tid, rebar_repo) == "in_progress"


# ── DET-floor block → no signature → claim still blocked ───────────────────────
def test_det_block_yields_no_signature(rebar_repo: Path) -> None:
    _commit(rebar_repo)
    _enable(rebar_repo)
    # No `## Acceptance Criteria` ⇒ P1 blocks ⇒ verdict BLOCK ⇒ not signed.
    tid = _make(rebar_repo, desc="A plan body with no acceptance criteria section at all here.")
    verdict = _review(tid, rebar_repo)
    assert verdict["verdict"] == "BLOCK" and not verdict["signature"]["signed"]
    with pytest.raises(rebar.RebarError):
        rebar.claim(tid, repo_root=str(rebar_repo))
    assert _status(tid, rebar_repo) == "open"


# ── REVIEW_RESULT sidecar is reducer-ignored ───────────────────────────────────
def test_sidecar_is_reducer_ignored(rebar_repo: Path) -> None:
    _commit(rebar_repo)
    tid = _make(rebar_repo)  # gate off; just exercise review_plan's sidecar emit
    verdict = _review(tid, rebar_repo)
    assert verdict["sidecar_emitted"] is True
    # The ticket is still readable and its status is unaffected by the sidecar event.
    st = rebar.show_ticket(tid, repo_root=str(rebar_repo))
    assert st["status"] == "open"
    # A REVIEW_RESULT event file exists on disk (preserved) but is not in compiled state.
    tracker = Path(_config.tracker_dir(str(rebar_repo)))
    from rebar._engine_support.resolver import resolve_ticket_id

    rid = resolve_ticket_id(tid, str(tracker))
    files = list((tracker / rid).glob("*-REVIEW_RESULT.json"))
    assert files, "REVIEW_RESULT event was not written"
    from rebar.reducer._version import is_unknown_newer_type

    assert is_unknown_newer_type("REVIEW_RESULT") is False  # fsck recognises it (no warn)


# ── the claim path makes NO LLM/network call (the 50ms-target structural proof) ─
def test_claim_path_makes_no_llm_call(rebar_repo: Path, monkeypatch) -> None:
    _commit(rebar_repo)
    _enable(rebar_repo)
    tid = _make(rebar_repo)
    _review(tid, rebar_repo)  # earn the signature (this is the out-of-band part)

    # Now poison the LLM op: if the CLAIM path touches it, the test fails.
    def _boom(*a, **k):
        raise AssertionError("claim path must NOT make an LLM call")

    monkeypatch.setattr(rebar.llm, "review_plan", _boom)
    monkeypatch.setattr(rebar.llm, "verify_completion", _boom)
    rebar.claim(tid, repo_root=str(rebar_repo))  # pure local HMAC verify — no LLM
    assert _status(tid, rebar_repo) == "in_progress"


# ── REVIEW_RESULT retention prune bounds growth (db7b AC4) ──────────────────────
def test_sidecar_prune_bounds_growth(rebar_repo: Path) -> None:
    from rebar._engine_support.resolver import resolve_ticket_id
    from rebar.llm.plan_review import sidecar

    _commit(rebar_repo)
    tid = _make(rebar_repo)
    # Emit more sidecars than the retention bound; prune keeps the most-recent `keep`.
    for _ in range(5):
        sidecar.emit(
            {
                "ticket_id": tid,
                "verdict": "PASS",
                "coverage": {},
                "blocking": [],
                "advisory": [],
                "overflow": [],
                "indeterminate": [],
                "dropped": [],
                "coaching": [],
            },
            repo_root=str(rebar_repo),
        )
    tracker = Path(_config.tracker_dir(str(rebar_repo)))
    rid = resolve_ticket_id(tid, str(tracker))
    sidecar.prune(tid, keep=2, repo_root=str(rebar_repo))
    remaining = list((tracker / rid).glob("*-REVIEW_RESULT.json"))
    assert len(remaining) == 2, f"prune should retain 2, found {len(remaining)}"
    # The ticket is still readable (reducer-ignored events never affect state).
    assert rebar.show_ticket(tid, repo_root=str(rebar_repo))["status"] == "open"


# ── LLM unavailable → fail LOUD, never a hollow PASS (fuel-posse-ball) ──────────
def test_review_fails_loud_when_deps_unavailable(rebar_repo: Path) -> None:
    # preflight raises (missing agents extra) ⇒ the LLM tier cannot run ⇒ INDETERMINATE
    # (NOT a DET-only PASS), and never signed.
    from rebar.llm.errors import LLMConfigError

    class _NoDeps:
        name = "no-deps"

        def preflight(self):
            raise LLMConfigError("the 'agents' extra is missing — install nava-rebar[agents]")

        def run(self, req):
            raise AssertionError("run must not be reached when preflight fails")

    _commit(rebar_repo)
    tid = _make(rebar_repo)
    v = rebar.llm.review_plan(tid, runner=_NoDeps(), repo_root=str(rebar_repo))
    assert v["verdict"] == "INDETERMINATE"  # NOT PASS — no hollow pass
    assert v["coverage"]["llm_ran"] is False and v["coverage"].get("llm_unavailable") is True
    assert not v["signature"]["signed"]  # never signed when the tier did not run


def test_review_fails_loud_when_key_unavailable_at_runtime(rebar_repo: Path) -> None:
    # preflight passes (deps present) but the provider call fails (e.g. missing/invalid
    # API key) — surfaces as LLMUnavailableError from the runner ⇒ INDETERMINATE, unsigned,
    # claim stays blocked. Covers any provider, not just Anthropic.
    from rebar.llm.errors import LLMUnavailableError

    class _NoKey:
        name = "no-key"

        def preflight(self):
            return None  # deps fine

        def run(self, req):
            raise LLMUnavailableError("the LLM provider call failed: OPENAI_API_KEY not set")

    _commit(rebar_repo)
    _enable(rebar_repo)
    tid = _make(rebar_repo)
    v = rebar.llm.review_plan(tid, runner=_NoKey(), repo_root=str(rebar_repo))
    assert v["verdict"] == "INDETERMINATE" and not v["signature"]["signed"]
    assert v["coverage"].get("llm_unavailable") is True
    with pytest.raises(rebar.RebarError):  # no attestation earned → claim blocked
        rebar.claim(tid, repo_root=str(rebar_repo))
    assert _status(tid, rebar_repo) == "open"


def test_workflow_surfaces_unavailable_llm_as_failed_step(rebar_repo: Path) -> None:
    # The shared contract holds for the OTHER prompt-using client: a workflow whose agent
    # step hits an unavailable LLM reports the run FAILED (not a silently-empty success).
    from rebar.llm.errors import LLMUnavailableError
    from rebar.llm.workflow import executor as _wf

    class _NoKeyAgent(_wf.AgentStepRunner):
        def run(self, ctx):
            raise LLMUnavailableError("the LLM provider call failed: ANTHROPIC_API_KEY not set")

    doc = {
        "schema_version": "1",
        "name": "wf",
        "steps": [{"id": "s1", "prompt": "code-quality", "mode": "text", "with": {}}],
    }
    res = _wf.run_workflow(doc, agent_runner=_NoKeyAgent(), repo_root=str(rebar_repo))
    assert res.status == "failed" and res.error  # surfaced, not swallowed into success


def test_per_criterion_failure_is_fail_open_when_tier_ran(rebar_repo: Path) -> None:
    # Fail-open PRESERVED at the LLM tier: a NON-systemic per-criterion failure (the tier
    # RAN; a finder raised an ordinary error, not LLMUnavailableError) drops that unit's
    # findings but does NOT mark the tier unavailable → still PASS + signed, claim succeeds.
    # (Distinguishes a systemic outage, which is INDETERMINATE, from a one-off hiccup.)
    # The hiccup is per-FINDER (verify/coach still resolve), so the tier RAN — the runner is
    # available and only the finder calls raise a non-systemic error.
    flaky = _GateFake(finder_error=ValueError("transient parse hiccup for one criterion"))

    _commit(rebar_repo)
    _enable(rebar_repo)
    tid = _make(rebar_repo)
    v = rebar.llm.review_plan(tid, runner=flaky, repo_root=str(rebar_repo))
    assert v["verdict"] == "PASS"  # tier ran (no systemic failure) → NOT INDETERMINATE
    assert v["coverage"]["llm_ran"] is True
    assert v["coverage"].get("llm_unavailable") is not True
    assert v["signature"]["signed"]
    rebar.claim(tid, assignee="me", repo_root=str(rebar_repo))
    assert _status(tid, rebar_repo) == "in_progress"


# ── E2E edge case: cap-hit INDETERMINATE (budget shed) ──────────────────────────
def test_review_cap_hit_indeterminate(rebar_repo: Path, monkeypatch) -> None:
    monkeypatch.setenv("REBAR_PLAN_REVIEW_BUDGET", "0")  # near-zero cap ⇒ shed agent/overlay
    _commit(rebar_repo)
    tid = _make(rebar_repo, "story")
    v = rebar.llm.review_plan(tid, runner=_CLEAN, repo_root=str(rebar_repo), sign=False)
    # The shared shed_to_budget (run by the workflow's ProductionBatchRunner via run_pass1)
    # sheds the lowest-priority AGENT/overlay criteria at cap 0; each shed criterion is
    # emitted as a non-blocking INDETERMINATE finding (the OBSERVABLE budget-shed behaviour).
    shed = [f for f in v["indeterminate"] if f.get("reason") == "budget-cap-shed"]
    assert shed, "expected agent/overlay criteria shed at cap 0"
    assert all(f.get("decision") != "block" for f in shed)  # shedding never blocks


# ── code-drift invalidation (epic boil-golem-veto / ADR 0002) ───────────────────
def _scoped(repo: Path, *, dep: str = "dep.py", content: str = "v = 1\n") -> str:
    """A claimable, reviewed ticket whose attestation is SCOPED to one dependency
    file (via file_impact). Returns the ticket id; the attestation is signed."""
    (repo / dep).write_text(content)
    _commit_all(repo)  # the reviewed file must exist in the attested snapshot
    tid = _make(repo)
    rebar.set_file_impact(
        tid, [{"path": dep, "reason": "the code under review"}], repo_root=str(repo)
    )
    v = _review(tid, repo)
    assert v["signature"]["signed"], "scoped ticket should earn a signed attestation"
    return tid


def test_code_drift_in_dependency_file_invalidates(rebar_repo: Path) -> None:
    _commit(rebar_repo)
    _enable(rebar_repo)
    tid = _scoped(rebar_repo)
    (rebar_repo / "dep.py").write_text("v = 2  # changed\n")  # drift in the reviewed file
    _commit_all(rebar_repo)  # drift is what LANDED — the attested basis moved
    with pytest.raises(rebar.RebarError) as ei:
        rebar.claim(tid, repo_root=str(rebar_repo))
    assert "drift" in ei.value.stderr.lower()
    assert _status(tid, rebar_repo) == "open"


def test_unrelated_change_does_not_invalidate_attestation(rebar_repo: Path) -> None:
    # The worm-folly-barge scenario: an unrelated commit (HEAD moves) must NOT stale a
    # still-correct, scoped attestation.
    _commit(rebar_repo)
    _enable(rebar_repo)
    tid = _scoped(rebar_repo)
    (rebar_repo / "unrelated.py").write_text("noise = True\n")
    _commit(rebar_repo)  # HEAD advances; dep.py is untouched
    rebar.claim(tid, assignee="me", repo_root=str(rebar_repo))
    assert _status(tid, rebar_repo) == "in_progress"


def test_dependency_file_deletion_invalidates(rebar_repo: Path) -> None:
    _commit(rebar_repo)
    _enable(rebar_repo)
    tid = _scoped(rebar_repo)
    (rebar_repo / "dep.py").unlink()  # deleting a reviewed file is drift
    _commit_all(rebar_repo)
    with pytest.raises(rebar.RebarError):
        rebar.claim(tid, repo_root=str(rebar_repo))
    assert _status(tid, rebar_repo) == "open"


def test_empty_dependency_set_falls_back_to_head(rebar_repo: Path) -> None:
    # No file_impact and (FakeRunner) no citations ⇒ unscopable ⇒ conservative
    # whole-HEAD freshness: any commit invalidates.
    _commit(rebar_repo)
    _enable(rebar_repo)
    tid = _make(rebar_repo)
    assert _review(tid, rebar_repo)["signature"]["signed"]
    _commit(rebar_repo)  # HEAD advances; nothing to scope to
    with pytest.raises(rebar.RebarError) as ei:
        rebar.claim(tid, repo_root=str(rebar_repo))
    assert "stale" in ei.value.stderr.lower()
    assert _status(tid, rebar_repo) == "open"


def test_claim_path_drift_check_is_cheap(rebar_repo: Path) -> None:
    # Times the DRIFT STEP itself (re-hashing the signed dependency paths) over ~30
    # files and asserts it stays in low single-digit ms — the AC's measurable bound.
    # (The no-LLM/no-network property of the claim path is pinned by
    # test_claim_path_makes_no_llm_call above.)
    from rebar import config as _config
    from rebar.llm.plan_review import attest

    _commit(rebar_repo)
    _enable(rebar_repo)
    impact = []
    for i in range(30):
        (rebar_repo / f"d{i}.py").write_text(f"x = {i}\n")
        impact.append({"path": f"d{i}.py", "reason": "r"})
    _commit_all(rebar_repo)  # the reviewed files must be IN the attested basis
    tid = _make(rebar_repo)
    rebar.set_file_impact(tid, impact, repo_root=str(rebar_repo))
    assert _review(tid, rebar_repo)["signature"]["signed"]

    # Recover the SIGNED {path: hash} map the claim path re-hashes, then time exactly
    # that comparison loop (what claim_gate_check does for drift).
    sig = rebar.verify_signature(tid, repo_root=str(rebar_repo))
    deps = attest.manifest_deps(sig["manifest"])
    assert len(deps) == 30
    base = str(_config.repo_root(str(rebar_repo)))

    def _drift_step() -> list[str]:
        return [p for p, h in deps.items() if attest._hash_file(p, base=base) != h]

    assert _drift_step() == []  # no drift → certified
    best = min(_timed(_drift_step) for _ in range(5))  # min-of-5 ⇒ intrinsic cost, not jitter
    assert best < 0.005, f"drift step too slow ({best * 1000:.2f}ms over 30 files)"


def _timed(fn) -> float:
    import time

    t0 = time.perf_counter()
    fn()
    return time.perf_counter() - t0


# ── progressive drift-refresh (Story 2, epic boil-golem-veto / ADR 0002) ────────
def test_drift_refresh_reuses_on_immaterial_drift(rebar_repo: Path) -> None:
    # A clean probe (FakeRunner finds nothing) means the drift didn't break the plan →
    # the attestation is REFRESHED (not fully re-reviewed) and the claim then succeeds.
    _commit(rebar_repo)
    _enable(rebar_repo)
    tid = _scoped(rebar_repo)  # signed; dep.py hashed into the manifest
    (rebar_repo / "dep.py").write_text("v = 1  # cosmetic edit; plan still holds\n")  # drift
    _commit_all(rebar_repo)
    v = _review(tid, rebar_repo)
    assert v["coverage"].get("drift_refresh") is True
    assert v["signature"].get("refreshed") is True and v["verdict"] == "PASS"
    rebar.claim(tid, assignee="me", repo_root=str(rebar_repo))
    assert _status(tid, rebar_repo) == "in_progress"


def test_refreshed_attestation_rebinds_to_current_code(rebar_repo: Path) -> None:
    # The refreshed attestation is re-bound to the CURRENT dependency hashes: a FURTHER
    # drift after the refresh staleness-blocks the claim (no stale reuse).
    _commit(rebar_repo)
    _enable(rebar_repo)
    tid = _scoped(rebar_repo)
    (rebar_repo / "dep.py").write_text("v = 2\n")
    _commit_all(rebar_repo)
    _review(tid, rebar_repo)  # refresh, now bound to "v = 2"
    (rebar_repo / "dep.py").write_text("v = 3\n")  # drift again
    _commit_all(rebar_repo)
    with pytest.raises(rebar.RebarError) as ei:
        rebar.claim(tid, repo_root=str(rebar_repo))
    assert "drift" in ei.value.stderr.lower()


def test_drift_refresh_skips_on_material_edit(rebar_repo: Path) -> None:
    # A ticket material edit is NOT a drift-only staleness → no refresh; a full review runs.
    _commit(rebar_repo)
    _enable(rebar_repo)
    tid = _scoped(rebar_repo)
    rebar.edit_ticket(
        tid,
        description=_DESC + "\nNEW materially-different requirement.",
        repo_root=str(rebar_repo),
    )
    v = _review(tid, rebar_repo)
    assert "drift_refresh" not in v["coverage"]  # full review, not the progressive path


def test_drift_refresh_skips_on_registry_skew(rebar_repo: Path, monkeypatch) -> None:
    # If the criteria registry changed since signing, the probe's meaning may differ →
    # fall back to a FULL re-review rather than refreshing.
    from rebar.llm.plan_review import attest

    _commit(rebar_repo)
    _enable(rebar_repo)
    tid = _scoped(rebar_repo)
    (rebar_repo / "dep.py").write_text("v = 9\n")  # drift
    monkeypatch.setattr(
        attest, "registry_version", lambda repo_root=None: "different-version-stamp"
    )
    v = _review(tid, rebar_repo)
    assert "drift_refresh" not in v["coverage"]


def test_drift_refresh_escalates_on_probe_finding(rebar_repo: Path, monkeypatch) -> None:
    # A probe that BLOCKS, or surfaces an advisory CITING a drifted file, means the plan may
    # no longer hold → drift_refresh escalates (returns None) so the caller runs the full
    # review. Drives the migrated seam: the probe now runs the workflow gate (PROBE MODE) via
    # gate_dispatch.produce_plan_review_verdict, not the retired bespoke _run_passes (WS1).
    # This is the parity corpus for the escalate decision (the refresh decision is pinned by
    # test_drift_refresh_reuses_on_immaterial_drift, which runs the real probe end-to-end).
    from rebar.llm.config import LLMConfig
    from rebar.llm.plan_review import orchestrator
    from rebar.llm.workflow import gate_dispatch

    _commit(rebar_repo)
    _enable(rebar_repo)
    tid = _scoped(rebar_repo)
    (rebar_repo / "dep.py").write_text("v = 42  # material change\n")  # dep.py now drifted
    _commit_all(rebar_repo)  # drift is what LANDED — the attested basis moved
    cfg = LLMConfig.from_env(repo_root=str(rebar_repo))
    ctx = orchestrator.assemble_context(tid, repo_root=str(rebar_repo), cfg=cfg)
    # A refresh RE-SIGNS, and signing requires an attested session (the no-null-pin
    # invariant, bug 5128-0856) — enter one the way review_plan would.
    from rebar.llm import gate_source

    handle = gate_source.resolve_gate_handle("HEAD", "attested", str(rebar_repo), fetch=False)

    def _verdict(*, blocking, advisory):
        return {
            "verdict": "BLOCK" if blocking else "PASS",
            "blocking": blocking,
            "advisory": advisory,
            "overflow": [],
            "coverage": {"llm_ran": True},
        }

    # (a) a blocking probe finding → escalate (verdict is BLOCK, not PASS).
    monkeypatch.setattr(
        gate_dispatch,
        "produce_plan_review_verdict",
        lambda *a, **k: _verdict(blocking=[{"decision": "block", "criteria": ["E4"]}], advisory=[]),
    )
    with gate_source.gate_read_root(handle):
        assert (
            orchestrator.drift_refresh(ctx, cfg, runner=_CLEAN, repo_root=str(rebar_repo)) is None
        )

    # (b) a PASS probe whose surfaced advisory CITES the drifted dep.py → escalate.
    monkeypatch.setattr(
        gate_dispatch,
        "produce_plan_review_verdict",
        lambda *a, **k: _verdict(
            blocking=[],
            advisory=[{"decision": "advisory", "citations": [{"kind": "file", "path": "dep.py"}]}],
        ),
    )
    with gate_source.gate_read_root(handle):
        assert (
            orchestrator.drift_refresh(ctx, cfg, runner=_CLEAN, repo_root=str(rebar_repo)) is None
        )

    # (c) a PASS probe whose advisory cites an UNDRIFTED file → NO escalation (refreshes).
    monkeypatch.setattr(
        gate_dispatch,
        "produce_plan_review_verdict",
        lambda *a, **k: _verdict(
            blocking=[],
            advisory=[
                {"decision": "advisory", "citations": [{"kind": "file", "path": "other.py"}]}
            ],
        ),
    )
    with gate_source.gate_read_root(handle):
        refreshed = orchestrator.drift_refresh(ctx, cfg, runner=_CLEAN, repo_root=str(rebar_repo))
    assert refreshed is not None and refreshed["coverage"].get("drift_refresh") is True


def test_drift_refresh_skips_when_no_prior_verdict(rebar_repo: Path) -> None:
    # First-time review (no prior attestation to reuse) → full review, never the
    # progressive path, even with the flag on.
    _commit(rebar_repo)
    _enable(rebar_repo)  # progressive on
    tid = _make(rebar_repo)
    v = _review(tid, rebar_repo)
    assert "drift_refresh" not in v["coverage"]
    assert v["verdict"] == "PASS" and v["signature"]["signed"]


def test_progressive_drift_refresh_always_takes_probe(rebar_repo: Path) -> None:
    # progressive drift-refresh is always on (off switch retired in story 4cdf) —
    # a drift-only-stale re-review takes the scoped probe path instead of a full re-review.
    _commit(rebar_repo)
    _enable(rebar_repo)
    tid = _scoped(rebar_repo)
    (rebar_repo / "dep.py").write_text("v = 1  # cosmetic\n")  # drift
    _commit_all(rebar_repo)
    v = _review(tid, rebar_repo)
    assert "drift_refresh" in v["coverage"]  # always-on: the scoped probe path is taken
    # the probe not only fires but REFRESHES the attestation (no full re-review)
    assert v["signature"].get("refreshed") is True and v["verdict"] == "PASS"


# The explicit-false back-out (progressive_drift_refresh = false → full re-review) was retired in
# story 4cdf; drift-refresh is now unconditional, so that scenario no longer exists and its test
# was removed.


# ── config: dotted enables, default off ─────────────────────────────────────────
def test_config_flag_default_off(tmp_path: Path) -> None:
    from rebar import config

    config.reset_config_cache()
    off = tmp_path / "off"
    off.mkdir()
    assert config.load_config(str(off)).verify.require_plan_review_for_claim is False

    config.reset_config_cache()
    on = tmp_path / "on"
    on.mkdir()
    (on / "rebar.toml").write_text("[verify]\nrequire_plan_review_for_claim = true\n")
    assert config.load_config(str(on)).verify.require_plan_review_for_claim is True


# ── bug mellow-coke-veto: the `signature` field is a {signed: bool} CONTRACT, not a
#    presence flag. A BLOCK verdict NEVER carries a passing signed attestation, and the
#    object is ALWAYS present (so a consumer that checks presence/truthiness — instead of
#    reading `.signed` — is the one that's misled, not the gate). These pin that contract.
_NO_AC_DESC = (
    "A detailed plan body that is comfortably over the length floor and reads like a real "
    "change description across several clauses, but DELIBERATELY omits the Acceptance "
    "Criteria block so the deterministic P1 readiness floor BLOCKS it with no LLM call."
)


def test_block_verdict_carries_no_passing_signature(rebar_repo: Path) -> None:
    """BLOCK → ``signature.signed`` is False. The ``signature`` object is still PRESENT
    (a non-null presence check is the documented footgun); ``.signed`` is the boolean of
    record, and no HMAC attestation event is written to the ticket."""
    _commit(rebar_repo)
    _enable(rebar_repo)
    tid = _make(rebar_repo, desc=_NO_AC_DESC)
    verdict = rebar.llm.review_plan(tid, runner=_CLEAN, source="local", repo_root=str(rebar_repo))
    assert verdict["verdict"] == "BLOCK"
    # The field is ALWAYS present — a presence/truthiness check would WRONGLY read "signed".
    assert verdict["signature"] is not None
    # …but `.signed` is the trustworthy boolean: False on a BLOCK.
    assert verdict["signature"]["signed"] is False
    # And no HMAC attestation event was written to the ticket itself.
    assert rebar.show_ticket(tid, repo_root=str(rebar_repo)).get("signature") in (None, {})


def test_pass_verdict_carries_a_passing_signature(rebar_repo: Path) -> None:
    """The mirror case: a genuine PASS DOES sign — ``signature.signed`` is True.

    Reviewed on the ATTESTED source. This test used to pass ``source="local"`` and assert a
    signature, which quietly encoded the melancholy-firstborn-shihtzu bypass as expected
    behavior: only an attested read is certifiable, so a local review must NOT sign."""
    _commit(rebar_repo)
    _enable(rebar_repo)
    tid = _make(rebar_repo)  # _DESC carries the AC block → the DET floor passes
    verdict = rebar.llm.review_plan(tid, runner=_CLEAN, repo_root=str(rebar_repo))
    assert verdict["verdict"] == "PASS"
    assert verdict["signature"]["signed"] is True


def test_block_verdict_cannot_satisfy_the_claim_gate(rebar_repo: Path) -> None:
    """AC#2: a BLOCKed plan is never claimable on the strength of its (non-)signature —
    reviewing it leaves the claim gate unsatisfied, so the claim stays blocked."""
    _commit(rebar_repo)
    _enable(rebar_repo)
    tid = _make(rebar_repo, desc=_NO_AC_DESC)
    verdict = rebar.llm.review_plan(tid, runner=_CLEAN, source="local", repo_root=str(rebar_repo))
    assert verdict["verdict"] == "BLOCK"
    with pytest.raises(rebar.RebarError) as ei:
        rebar.claim(tid, assignee="me", repo_root=str(rebar_repo))
    assert ei.value.returncode == 1
    assert _status(tid, rebar_repo) == "open"  # never claimed off a BLOCK


# ── idempotence short-circuit (feature b3e5) ────────────────────────────────────
class _CountingFake(_GateFake):
    """A ``_GateFake`` that counts how many times the runner is invoked, so a test can
    assert the multi-pass LLM review did (or did NOT) run on a given ``review_plan`` call."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def run(self, req) -> dict:  # type: ignore[override]
        self.calls += 1
        return super().run(req)


def test_idempotent_second_review_skips_the_llm(rebar_repo: Path) -> None:
    # A first signing review runs the LLM and signs. A SECOND review on the UNCHANGED
    # ticket must NOT invoke the LLM again — it short-circuits and reuses the attestation.
    _commit(rebar_repo)
    _enable(rebar_repo)
    tid = _make(rebar_repo)
    runner = _CountingFake()
    v1 = _review(tid, rebar_repo, runner=runner)
    assert v1["verdict"] == "PASS" and v1["signature"]["signed"]
    first = runner.calls
    assert first > 0  # the first review actually ran the passes

    v2 = _review(tid, rebar_repo, runner=runner)
    assert runner.calls == first  # no further LLM calls on the 2nd review
    assert v2["verdict"] == "PASS"
    assert v2["coverage"]["idempotent_skip"] is True
    assert v2["coverage"]["llm_ran"] is False
    assert v2["signature"]["signed"] is True  # reuses the current attestation


def test_force_reruns_the_llm_on_an_unchanged_ticket(rebar_repo: Path) -> None:
    # --force bypasses the idempotence short-circuit and forces a full re-review.
    _commit(rebar_repo)
    _enable(rebar_repo)
    tid = _make(rebar_repo)
    runner = _CountingFake()
    _review(tid, rebar_repo, runner=runner)
    first = runner.calls

    v2 = rebar.llm.review_plan(tid, runner=runner, repo_root=str(rebar_repo), force=True)
    assert runner.calls > first  # the LLM ran again under --force
    assert v2["coverage"].get("idempotent_skip") is not True


def test_material_change_reruns_the_llm(rebar_repo: Path) -> None:
    # A MATERIAL edit invalidates the attestation (fingerprint changes), so the 2nd review
    # is NOT idempotent and re-runs the LLM.
    _commit(rebar_repo)
    _enable(rebar_repo)
    tid = _make(rebar_repo)
    runner = _CountingFake()
    _review(tid, rebar_repo, runner=runner)
    first = runner.calls

    new_desc = _DESC + "\n\n## Extra\nAdditional scope detail that materially changes the plan.\n"
    rebar.edit_ticket(tid, description=new_desc, repo_root=str(rebar_repo))

    v2 = _review(tid, rebar_repo, runner=runner)
    assert runner.calls > first  # fingerprint changed → full re-review
    assert v2["coverage"].get("idempotent_skip") is not True


def test_skip_path_still_satisfies_the_claim_gate(rebar_repo: Path) -> None:
    # The skip did not weaken the gate: after a reuse (skip) review, the attestation is
    # still valid and a claim succeeds.
    _commit(rebar_repo)
    _enable(rebar_repo)
    tid = _make(rebar_repo)
    assert _review(tid, rebar_repo)["signature"]["signed"]  # sign
    v2 = _review(tid, rebar_repo)  # skip path
    assert v2["coverage"]["idempotent_skip"] is True
    rebar.claim(tid, assignee="me", repo_root=str(rebar_repo))
    assert _status(tid, rebar_repo) == "in_progress"


# ── cheap re-sign path (ticket middle-actinium-thrush) ──────────────────────────
def _review_no_sign(tid: str, repo: Path, runner=_CLEAN):
    """Simulate 'review computed a signable PASS but the SIGN step failed to persist the
    attestation': run the full review (which emits the REVIEW_RESULT sidecar) but do NOT
    sign, so the ticket carries the recorded verdict yet no attestation."""
    return rebar.llm.review_plan(tid, runner=runner, sign=False, repo_root=str(repo))


def test_resign_recovers_lost_attestation_without_llm(rebar_repo: Path, monkeypatch) -> None:
    # The core AC: a computed PASS whose signature was lost is recovered CHEAPLY — the sidecar
    # is re-signed with NO LLM call, so the claim gate (which failed) then passes.
    from rebar.llm.plan_review import attest
    from rebar.llm.workflow import gate_dispatch

    _commit(rebar_repo)
    _enable(rebar_repo)
    tid = _make(rebar_repo)
    runner = _CountingFake()
    v = _review_no_sign(tid, rebar_repo, runner=runner)
    assert v["verdict"] == "PASS" and v["sidecar_emitted"] is True
    assert v["signature"]["signed"] is False  # the "sign failed / not signed" state
    calls_after_review = runner.calls
    assert calls_after_review > 0  # the review actually ran the passes

    # With no valid attestation, the claim gate FAILS.
    assert attest.claim_gate_check(tid, repo_root=str(rebar_repo))["ok"] is False

    # Poison the multi-pass pipeline: the cheap re-sign must NEVER run the LLM review.
    def _boom(*a, **k):
        raise AssertionError("sign-review must NOT run the multi-pass LLM review")

    monkeypatch.setattr(gate_dispatch, "produce_plan_review_verdict", _boom)

    res = rebar.llm.resign_plan_review(tid, repo_root=str(rebar_repo))
    assert res["ok"] is True and res["signed"] is True
    assert runner.calls == calls_after_review  # the re-sign invoked NO runner

    # The attestation is now persisted → the claim gate PASSES and a claim succeeds.
    assert attest.claim_gate_check(tid, repo_root=str(rebar_repo))["ok"] is True
    rebar.claim(tid, assignee="me", repo_root=str(rebar_repo))
    assert _status(tid, rebar_repo) == "in_progress"


def test_resign_refuses_when_plan_changed(rebar_repo: Path) -> None:
    # The staleness guard: a material edit AFTER the review makes the recorded verdict stale,
    # so the cheap re-sign REFUSES (no signature written) rather than sign a stale verdict.
    from rebar.llm.plan_review import attest

    _commit(rebar_repo)
    _enable(rebar_repo)
    tid = _make(rebar_repo)
    _review_no_sign(tid, rebar_repo)
    rebar.edit_ticket(
        tid,
        description=_DESC + "\nNEW materially-different requirement.",
        repo_root=str(rebar_repo),
    )
    res = rebar.llm.resign_plan_review(tid, repo_root=str(rebar_repo))
    assert res["ok"] is False and res["signed"] is False
    assert "changed" in res["reason"].lower()  # names the stale-plan cause
    # No signature written → the claim gate stays blocked.
    assert attest.claim_gate_check(tid, repo_root=str(rebar_repo))["ok"] is False
    with pytest.raises(rebar.RebarError):
        rebar.claim(tid, repo_root=str(rebar_repo))
    assert _status(tid, rebar_repo) == "open"


def test_resign_refuses_when_no_pass_sidecar(rebar_repo: Path) -> None:
    # No sidecar at all → refuse; a BLOCK sidecar → refuse (never sign a non-PASS).
    _commit(rebar_repo)
    _enable(rebar_repo)
    never_reviewed = _make(rebar_repo)
    res = rebar.llm.resign_plan_review(never_reviewed, repo_root=str(rebar_repo))
    assert res["ok"] is False and res["signed"] is False
    assert "no REVIEW_RESULT sidecar" in res["reason"]

    blocked = _make(rebar_repo, desc=_NO_AC_DESC)  # DET floor BLOCKS → sidecar records BLOCK
    v = rebar.llm.review_plan(blocked, runner=_CLEAN, source="local", repo_root=str(rebar_repo))
    assert v["verdict"] == "BLOCK"
    res = rebar.llm.resign_plan_review(blocked, repo_root=str(rebar_repo))
    assert res["ok"] is False and res["signed"] is False
    assert "not a signable PASS" in res["reason"]


def test_sign_review_cli_success_and_refusal(rebar_repo: Path, capsys) -> None:
    # The `rebar sign-review` CLI: exit 0 on a successful cheap re-sign (claim gate then
    # passes), exit 1 on a refusal (a ticket that was never reviewed).
    from rebar.llm.plan_review import attest

    _commit(rebar_repo)
    _enable(rebar_repo)
    tid = _make(rebar_repo)
    _review_no_sign(tid, rebar_repo)
    assert _cli.main(["sign-review", tid]) == 0
    assert attest.claim_gate_check(tid, repo_root=str(rebar_repo))["ok"] is True

    capsys.readouterr()  # drain the success output
    other = _make(rebar_repo)  # never reviewed → refusal
    assert _cli.main(["sign-review", other, "-o", "text"]) == 1
    assert "refused" in capsys.readouterr().err.lower()  # text refusal goes to stderr


# ── bug 406f: a plan naming a REAL third-party symbol yields no absence-BLOCK ────
#
# End-to-end pipeline regression (finder → verify → decide → verdict), NOT a
# resolver-level unit test. A Pass-1 finder flags a "symbol does not exist /
# hallucinated" BLOCK-class finding on a third-party symbol the plan names. The
# Pass-2 verifier (which the real gate runs AGENTICALLY with the `resolve_symbol`
# tool) re-grounds the cited symbol against the merged grounding oracle: because the
# symbol IS importable, the oracle REFUTES the asserted absence, the verifier marks
# `cited_reference_accurate=no`, and Pass-3 DROPS the finding — so no absence-BLOCK
# survives in `verdict["blocking"]`. Deterministic: no live LLM; the oracle's
# environment resolution runs even on ctags-blind hosts (the default CI condition).
#
# `yaml.safe_load` is used deliberately — PyYAML is a CORE dependency (installed on
# every CI job), so the third-party path is exercised everywhere, not only on
# `[agents]` jobs.

_TP_SYMBOL = "yaml.safe_load"  # a real, importable core-dependency member
_ABSENT_SYMBOL = "yaml.this_symbol_does_not_exist_zzz406f"  # importable module, missing attr

_PLAN_WITH_TP_SYMBOL = (
    "Body with enough length to be a real plan, describing the change in detail so the gate has "
    "something to review and the clarity heuristic is satisfied across the board here. The plan "
    f"parses config by calling `{_TP_SYMBOL}` on the untrusted input stream.\n\n"
    "## Acceptance Criteria\n- [ ] config is parsed safely\n- [ ] another verifiable check\n\n"
    "## Why\nx\n## What\ny\n## Scope\nz\n## Testing\nRun `pytest -q`.\n"
)


def _absence_finding(symbol: str) -> dict:
    """A BLOCK-class 'symbol does not exist / hallucinated' finding on E4 (a
    codebase-grounded, blocking-enabled criterion), citing ``symbol`` in evidence."""
    return {
        "finding": f"The plan references `{symbol}`, but this symbol does not exist in the "
        "codebase — it looks hallucinated / a missing edit target.",
        "criteria": ["E4"],
        "evidence": [f"symbol:{symbol}", "no definition found via Grep of the repo tree"],
        "location": "plan description",
        "checklist_item": f"- [ ] confirm `{symbol}` exists before committing to it",
        "impact": "Committing to a nonexistent symbol would break the build.",
    }


class _GroundingVerifierFake(_GateFake):
    """A ``_GateFake`` whose Pass-2 verifier RE-GROUNDS each finding's cited symbol
    against the real grounding oracle — the deterministic stand-in for the agentic
    verifier's ``resolve_symbol`` tool. When the oracle REFUTES the asserted absence
    (the symbol is importable), it marks ``cited_reference_accurate=no`` (the veto
    Pass-3 drops on); otherwise it returns a high-severity accurate verification so a
    genuinely-absent symbol still BLOCKs."""

    _SYMBOL_RE = re.compile(r"symbol:([^\s|]+)")
    _INDEX_RE = re.compile(r"### finding index (\d+)")

    def __init__(self, *, finder_findings=None, repo_root: str = "."):
        super().__init__(finder_findings=finder_findings)
        self._repo_root = repo_root

    @staticmethod
    def _verification(index: int, cited_reference_accurate: str) -> dict:
        # High PLAN-severity so a NON-dropped finding clears E4's 0.75 block threshold —
        # the negative control must be able to actually BLOCK. `impact_plan` reads the
        # plan-severity axes (NOT the code-review keys); `divergent_implementation`
        # (a hard-override axis: "the plan will build the wrong thing") graded
        # "contradicts_reality" — a plan-v4 FLOOR grade — yields impact 1.0, so
        # priority = validity × impact clears the threshold. The axis moved off the ordinal
        # none|low|medium|high ladder onto a closed kind set in plan-v4 (story
        # doggish-nonorganic-tsetsefly); "high" is no longer a valid grade.
        return {
            "index": index,
            "severity_attributes": {
                "divergent_implementation": "contradicts_reality",
            },
            "binary": {
                "cited_reference_accurate": cited_reference_accurate,
                "is_verifiable": "yes",
                "evidence_entails_finding": "yes",
                "path_reachable": "yes",
                "impact_follows_necessarily": "yes",
                "no_viable_alternative_explanation": "yes",
                "no_existing_mitigation": "yes",
                "severity_claim_justified": "yes",
            },
        }

    def run(self, req) -> dict:  # type: ignore[override]
        if req.output_schema != "plan_review_verification":
            return super().run(req)
        from rebar import grounding
        from rebar.llm import findings as _f

        verifs = []
        for block in re.split(r"(?=### finding index \d+)", req.instructions or ""):
            im = self._INDEX_RE.search(block)
            if not im:
                continue
            idx = int(im.group(1))
            sym = self._SYMBOL_RE.search(block)
            cra = "na"
            if sym:
                # Exactly what the agentic verifier does via resolve_symbol: consult
                # the installed environment to see whether the "absent" symbol exists.
                ev = grounding.refute_absence(
                    {"kind": "member", "name": sym.group(1), "language": "python"},
                    repo_root=self._repo_root,
                )
                cra = "no" if ev.get("outcome") == "refuted" else "yes"
            verifs.append(self._verification(idx, cra))
        payload = _f.validate_structured({"verifications": verifs}, "plan_review_verification")
        return {**payload, "runner": self.name, "model": None, "trace_id": None}


def test_third_party_symbol_absence_finding_is_refuted_no_block(rebar_repo: Path) -> None:
    """AC (406f): a plan naming a known-importable third-party symbol produces NO
    absence/does-not-exist BLOCK — the grounding refutation drops the finding."""
    _commit(rebar_repo)
    tid = _make(rebar_repo, desc=_PLAN_WITH_TP_SYMBOL)
    runner = _GroundingVerifierFake(
        finder_findings=[_absence_finding(_TP_SYMBOL)], repo_root=str(rebar_repo)
    )
    verdict = _review(tid, rebar_repo, runner=runner)

    # No blocking finding of the absence/does-not-exist class survives.
    def _is_absence_block(f: dict) -> bool:
        return "does not exist" in f.get("finding", "") or "hallucinat" in f.get("finding", "")

    assert not any(_is_absence_block(f) for f in verdict["blocking"]), (
        f"a third-party symbol wrongly produced an absence-BLOCK: {verdict['blocking']}"
    )
    assert verdict["verdict"] == "PASS"
    # The finding was DROPPED by the cited-reference-inaccurate veto (grounding refuted it).
    dropped_reasons = [f.get("reason") for f in verdict["dropped"]]
    assert "veto:cited-reference-inaccurate" in dropped_reasons, (
        f"absence finding not dropped by grounding refutation: {verdict['dropped']}"
    )


def test_genuinely_absent_symbol_still_blocks_control(rebar_repo: Path) -> None:
    """Non-vacuity control: the SAME finder finding on a genuinely-absent symbol (the
    oracle cannot refute it) DOES block — proving the pipeline can emit an
    absence-BLOCK and that grounding refutation is what spares the real symbol."""
    _commit(rebar_repo)
    tid = _make(rebar_repo, desc=_PLAN_WITH_TP_SYMBOL)
    runner = _GroundingVerifierFake(
        finder_findings=[_absence_finding(_ABSENT_SYMBOL)], repo_root=str(rebar_repo)
    )
    verdict = _review(tid, rebar_repo, runner=runner)
    assert verdict["verdict"] == "BLOCK"
    assert any("does not exist" in f.get("finding", "") for f in verdict["blocking"]), (
        "expected the absence finding to BLOCK for a truly-absent symbol; "
        f"blocking={verdict['blocking']}"
    )


# The verify.progressive_drift_refresh config key (default-True + its explicit-false back-out) was
# retired in story 4cdf: drift-refresh is now unconditional, the field is gone from VerifyConfig,
# and its config-default test was removed.


# ── read-only currency query: plan_review_status + `review-plan --status` (report #2) ──
def test_plan_review_status_certified_after_review(rebar_repo: Path) -> None:
    # The report's missing primitive: a no-LLM "is this approved right now?" read.
    _commit(rebar_repo)
    _enable(rebar_repo)
    tid = _make(rebar_repo)
    assert _review(tid, rebar_repo)["signature"]["signed"]
    st = rebar.llm.plan_review_status(tid, repo_root=str(rebar_repo))
    assert st["ok"] is True and st["verdict"] == "certified"
    assert st["verified_at_sha"]  # the bound base ref is surfaced, not just the verdict
    assert st["signed_at"]


def test_plan_review_status_absent_is_unsigned(rebar_repo: Path) -> None:
    _commit(rebar_repo)
    _enable(rebar_repo)
    tid = _make(rebar_repo)  # never reviewed
    st = rebar.llm.plan_review_status(tid, repo_root=str(rebar_repo))
    assert st["ok"] is False and st["verdict"] == "unsigned"
    assert st["verified_at_sha"] is None and st["signed_at"] is None


def test_plan_review_status_goes_stale_when_base_moves(rebar_repo: Path) -> None:
    # The report's core scenario: origin/main advances after the review. A read-only status
    # query must report the attestation as no-longer-current WITHOUT running a new review.
    _commit(rebar_repo)
    _enable(rebar_repo)
    tid = _make(rebar_repo)
    assert _review(tid, rebar_repo)["signature"]["signed"]
    _commit(rebar_repo)  # base ref moves; unscoped whole-HEAD freshness now fails
    st = rebar.llm.plan_review_status(tid, repo_root=str(rebar_repo))
    assert st["ok"] is False and "stale" in st["verdict"]


def test_review_plan_status_cli_exit_codes(rebar_repo: Path, capsys) -> None:
    import json

    _commit(rebar_repo)
    _enable(rebar_repo)
    tid = _make(rebar_repo)
    # absent → not current → distinct exit 12 (not a review's 0/1/2)
    assert _cli.main(["review-plan", tid, "--status", "-o", "text"]) == 12
    out = capsys.readouterr().out
    assert "PLAN REVIEW STATUS" in out and "current=False" in out
    # after a review → current → exit 0, and the bound sha is surfaced in json
    assert _review(tid, rebar_repo)["signature"]["signed"]
    assert _cli.main(["review-plan", tid, "--status", "-o", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True and payload["verdict"] == "certified"
    assert payload["verified_at_sha"]


# ── idempotence short-circuit is visible in text output (report #6) ─────────────
def test_render_plan_review_text_marks_idempotent_reuse(capsys) -> None:
    from rebar._cli._llm_commands import _render_plan_review_text

    _render_plan_review_text(
        {
            "verdict": "PASS",
            "ticket_id": "t",
            "runner": "reused",
            "coverage": {"llm_ran": False, "idempotent_skip": True, "counts": {}},
            "blocking": [],
            "advisory": [],
            "coaching": [],
        }
    )
    out = capsys.readouterr().out
    assert "PASS" in out and "reused" in out


def test_render_plan_review_text_fresh_has_no_reuse_marker(capsys) -> None:
    from rebar._cli._llm_commands import _render_plan_review_text

    _render_plan_review_text(
        {
            "verdict": "PASS",
            "ticket_id": "t",
            "coverage": {"counts": {}},
            "blocking": [],
            "advisory": [],
            "coaching": [],
        }
    )
    assert "reused" not in capsys.readouterr().out


def test_review_plan_cli_text_shows_reuse_on_second_run(rebar_repo: Path, capsys) -> None:
    # End-to-end: a first review signs; a second CLI review of the UNCHANGED ticket takes the
    # b3e5 short-circuit (no runner needed — it never reaches the LLM) and the text output says so.
    _commit(rebar_repo)
    _enable(rebar_repo)
    tid = _make(rebar_repo)
    assert _review(tid, rebar_repo)["signature"]["signed"]
    assert _cli.main(["review-plan", tid, "-o", "text"]) == 0
    assert "reused" in capsys.readouterr().out


# ── epic 58f2: sign-on-PASS is a DEFENDED invariant at each public boundary ─────
#
# The signing default was already correct on both public plan-review routes, but nothing
# pinned it at the REAL parser/API boundary — a refactor could silently flip it with no test
# failing. These tests make the default load-bearing: they assert the flag→kwarg mapping
# through the actual CLI parser, the library's `sign=True` default, the end-to-end
# review→claim loop over the real CLI route, the never-sign guard table, and the idempotent
# reuse path's promise that it emits no SECOND signature.


def _capture_review_plan_kwargs(monkeypatch) -> dict:
    """Swap `llm.review_plan` for a recorder and return the dict it fills in.

    `_llm_commands` does `from rebar import llm` INSIDE each command function, so the
    attribute is resolved on the module object at call time — patching it here is a genuine
    parser-boundary interception, not a re-implementation of the dispatch."""
    import rebar.llm as _llm_mod

    seen: dict = {}

    def _recorder(ticket_id, **kwargs):
        seen["ticket_id"] = ticket_id
        seen.update(kwargs)
        return {
            "verdict": "PASS",
            "ticket_id": ticket_id,
            "blocking": [],
            "advisory": [],
            "coaching": [],
            "coverage": {"counts": {}},
            "signature": {"signed": True},
        }

    monkeypatch.setattr(_llm_mod, "review_plan", _recorder)
    return seen


def test_cli_review_plan_signs_by_default_at_the_parser_boundary(rebar_repo: Path, monkeypatch):
    """AC3 (default half): omitting `--no-sign` reaches the library as `sign=True`."""
    _commit(rebar_repo)
    _enable(rebar_repo)
    tid = _make(rebar_repo)
    seen = _capture_review_plan_kwargs(monkeypatch)
    assert _cli.main(["review-plan", tid]) == 0
    assert seen["sign"] is True  # the default is SIGN, asserted through the real parser


def test_cli_review_plan_no_sign_is_the_only_way_to_skip(rebar_repo: Path, monkeypatch):
    """AC3 (opt-out half): unsigned execution requires the EXPLICIT flag."""
    _commit(rebar_repo)
    _enable(rebar_repo)
    tid = _make(rebar_repo)
    seen = _capture_review_plan_kwargs(monkeypatch)
    assert _cli.main(["review-plan", tid, "--no-sign"]) == 0
    assert seen["sign"] is False


def test_library_review_plan_signs_by_default(rebar_repo: Path) -> None:
    """AC1/AC4 (default half): the library default persists a CLAIM-VALID attestation —
    asserted on the ticket itself, not just on the returned verdict."""
    from rebar.llm.plan_review import attest

    _commit(rebar_repo)
    _enable(rebar_repo)
    tid = _make(rebar_repo)
    verdict = rebar.llm.review_plan(tid, runner=_CLEAN, repo_root=str(rebar_repo))
    assert verdict["verdict"] == "PASS" and verdict["signature"]["signed"] is True
    # Persisted on the TICKET (the durable artifact), and accepted by the claim gate.
    assert rebar.show_ticket(tid, repo_root=str(rebar_repo)).get("signature")
    assert attest.claim_gate_check(tid, repo_root=str(rebar_repo))["ok"] is True


def test_library_review_plan_sign_false_leaves_the_claim_gate_blocked(rebar_repo: Path) -> None:
    """AC4 (opt-out half): `sign=False` is the ONLY library route to an unsigned PASS, and it
    genuinely withholds the gate artifact — the claim stays blocked."""
    from rebar.llm.plan_review import attest

    _commit(rebar_repo)
    _enable(rebar_repo)
    tid = _make(rebar_repo)
    verdict = rebar.llm.review_plan(tid, runner=_CLEAN, sign=False, repo_root=str(rebar_repo))
    assert verdict["verdict"] == "PASS"
    assert verdict["signature"]["signed"] is False
    assert attest.claim_gate_check(tid, repo_root=str(rebar_repo))["ok"] is False
    with pytest.raises(rebar.RebarError) as ei:
        rebar.claim(tid, assignee="me", repo_root=str(rebar_repo))
    assert ei.value.returncode == 1
    assert _status(tid, rebar_repo) == "open"


def test_cli_review_plan_earns_a_claim_without_a_second_llm_call(rebar_repo: Path, monkeypatch):
    """AC2: the full loop over the REAL CLI route — `rebar review-plan <id>` with no opt-out
    signs an attestation bound to the current material, and the subsequent `rebar claim`
    is satisfied by it WITHOUT running the LLM again."""
    from rebar.llm import runner as _runner_mod
    from rebar.llm.plan_review import attest
    from rebar.llm.plan_review import orchestrator as _orch

    _commit(rebar_repo)
    _enable(rebar_repo)
    tid = _make(rebar_repo)

    counting = _CountingFake()
    # `orchestrator` binds `get_runner` at module import, so patching the runner module alone
    # would miss it; patch both to guarantee no live backend is reachable from this test.
    monkeypatch.setattr(_runner_mod, "get_runner", lambda cfg, override=None: counting)
    monkeypatch.setattr(_orch, "get_runner", lambda cfg, override=None: counting)

    assert _cli.main(["review-plan", tid]) == 0
    assert counting.calls > 0  # the CLI route really ran the review
    after_review = counting.calls

    assert attest.claim_gate_check(tid, repo_root=str(rebar_repo))["ok"] is True
    rebar.claim(tid, assignee="me", repo_root=str(rebar_repo))
    assert _status(tid, rebar_repo) == "in_progress"
    assert counting.calls == after_review  # the CLAIM itself is a pure local verify


def test_never_sign_guard_table(rebar_repo: Path) -> None:
    """AC5: the verdicts that must NEVER carry a passing signature — all five in one table:
    BLOCK, INDETERMINATE, degraded (LLM outage), local-source, and explicit `sign=False`.

    Each row goes through the public `review_plan` entry point end-to-end. The degraded row
    is the LLM-outage flavor (runner raises `LLMUnavailableError` → the review degrades and
    must not sign); the primitive-level degraded guard (`resolution_class` on the coverage
    block) is additionally pinned at the artifact that owns it by
    `test_sign_plan_review_refuses_non_certifiable_verdicts`. The local-source row also
    checks the machine-readable POLICY reason (bug melancholy-firstborn-shihtzu: local reads
    the in-place checkout and is never certifiable)."""
    from rebar.llm.errors import LLMUnavailableError

    _commit(rebar_repo)
    _enable(rebar_repo)

    class _Outage(FakeRunner):
        def run(self, request):
            raise LLMUnavailableError("the LLM provider call failed: ANTHROPIC_API_KEY not set")

    cases = []

    # (a) BLOCK — the deterministic floor rejects a plan with no Acceptance Criteria.
    blocked = _make(rebar_repo, desc=_NO_AC_DESC)
    cases.append(
        ("det-block", rebar.llm.review_plan(blocked, runner=_CLEAN, repo_root=str(rebar_repo)))
    )

    # (b) INDETERMINATE — a not-yet-claimable ticket fast-fails with NO LLM call.
    unclaimable = _make(rebar_repo)
    rebar.transition(unclaimable, "open", "blocked", repo_root=str(rebar_repo))
    cases.append(
        (
            "not-claimable",
            rebar.llm.review_plan(unclaimable, runner=_CLEAN, repo_root=str(rebar_repo)),
        )
    )

    # (c) DEGRADED — the LLM tier resolves abnormally (provider outage) mid-review.
    outage = _make(rebar_repo)
    cases.append(
        (
            "degraded-outage",
            rebar.llm.review_plan(outage, runner=_Outage(), repo_root=str(rebar_repo)),
        )
    )

    # (d) LOCAL SOURCE — a genuine PASS against the in-place checkout: never certifiable.
    local = _make(rebar_repo)
    cases.append(
        (
            "local-source",
            rebar.llm.review_plan(local, runner=_CLEAN, source="local", repo_root=str(rebar_repo)),
        )
    )

    # (e) explicit opt-out on an otherwise perfectly signable PASS.
    opted_out = _make(rebar_repo)
    cases.append(
        (
            "sign-false",
            rebar.llm.review_plan(opted_out, runner=_CLEAN, sign=False, repo_root=str(rebar_repo)),
        )
    )

    for label, verdict in cases:
        # `.signed` is the boolean of record; the object itself is always present.
        assert verdict["signature"]["signed"] is False, f"{label} must not sign"
    # The local row is a POLICY refusal, not a failure: machine-readable reason required.
    local_verdict = dict(cases)["local-source"]
    assert local_verdict["verdict"] == "PASS"  # the review itself still passes…
    assert local_verdict["signature"]["reason"] == "local-source-never-signs"


def test_sign_plan_review_refuses_non_certifiable_verdicts(rebar_repo: Path) -> None:
    """AC5 (the DEGRADED clause): the guard is enforced by the signing primitive itself, so it
    holds for every caller — not just the ones the end-to-end table happens to exercise.

    A *degraded* run is the case the end-to-end table cannot reach cheaply: it needs the LLM
    tier to resolve abnormally, which the offline fake cannot produce on demand. Driving
    `sign_plan_review` directly pins the same guard at the artifact that owns it
    (`plan_review/attest.py`), where a `resolution_class` on the coverage block marks the run
    degraded."""
    from rebar.llm.plan_review import attest
    from rebar.signing import SigningError

    _commit(rebar_repo)
    _enable(rebar_repo)
    tid = _make(rebar_repo)

    def _verdict(**over) -> dict:
        base = {
            "verdict": "PASS",
            "ticket_id": tid,
            "blocking": [],
            "advisory": [],
            "coaching": [],
            "coverage": {"counts": {}},
        }
        base.update(over)
        return base

    refused = {
        # A degraded run is NOT certifiable even though the verdict letter reads PASS.
        "degraded": _verdict(coverage={"counts": {}, "resolution_class": "degraded"}),
        "block": _verdict(verdict="BLOCK"),
        "indeterminate": _verdict(verdict="INDETERMINATE"),
    }
    for label, verdict in refused.items():
        with pytest.raises(SigningError) as ei:
            attest.sign_plan_review(verdict, material="deadbeef", repo_root=str(rebar_repo))
        # The refusal names WHY, so an operator can tell a degrade from a genuine BLOCK.
        assert "refusing to sign" in str(ei.value), f"{label} must be refused by name"


def test_idempotent_reuse_emits_no_second_signature(rebar_repo: Path) -> None:
    """AC7 (the untested half): the short-circuit is already known not to re-run the LLM, but
    it must also not mint a DUPLICATE attestation. `signed_at` is the witness — a re-sign
    would move it."""
    _commit(rebar_repo)
    _enable(rebar_repo)
    tid = _make(rebar_repo)

    counting = _CountingFake()
    first = rebar.llm.review_plan(tid, runner=counting, repo_root=str(rebar_repo))
    assert first["verdict"] == "PASS" and first["signature"]["signed"] is True
    calls_after_first = counting.calls
    signed_at = rebar.show_ticket(tid, repo_root=str(rebar_repo))["signature"]["signed_at"]

    second = rebar.llm.review_plan(tid, runner=counting, repo_root=str(rebar_repo))
    assert second["coverage"]["idempotent_skip"] is True
    assert counting.calls == calls_after_first  # no LLM
    # …and no NEW signature event: the stored attestation is byte-identical, not re-minted.
    assert rebar.show_ticket(tid, repo_root=str(rebar_repo))["signature"]["signed_at"] == signed_at


def test_review_plan_help_states_that_pass_signs_by_default(capsys) -> None:
    """AC8: the help is the author's contract. It must say PASS SIGNS by default, so the
    `--no-sign` flag reads as the exception it is rather than as the way to get a signature."""
    with pytest.raises(SystemExit):
        _cli.main(["review-plan", "--help"])
    # argparse hard-wraps help at the terminal width, so collapse whitespace before matching
    # phrases — otherwise the assertion breaks on where the line happens to fold.
    help_text = " ".join(capsys.readouterr().out.lower().split())
    assert "--no-sign" in help_text
    # The default must be STATED, not merely implied by the flag's existence.
    assert "by default a non-blocking pass signs one" in help_text
    # …and the help must name the cheap recovery path for a signature that was lost.
    assert "sign-review" in help_text


# ── melancholy-firstborn-shihtzu: an UNATTESTED read is never certifiable ───────
#
# `--source local` reads the in-place checkout, uncommitted edits included. It used to sign a
# fully claim-valid attestation bound to `verified_at_sha=None` — a dirty worktree could mint
# the artifact the claim gate trusts. ADR 0005 and `_snapshot.repo_snapshot.SOURCE_LOCAL` both
# document local as "dirty allowed, never signed"; the completion close gate already enforced
# it. These pin the plan-review half.


def test_local_source_pass_is_never_signed(rebar_repo: Path) -> None:
    """A local-source PASS is a real PASS that deliberately carries NO signature."""
    _commit(rebar_repo)
    _enable(rebar_repo)
    tid = _make(rebar_repo)
    verdict = rebar.llm.review_plan(tid, runner=_CLEAN, source="local", repo_root=str(rebar_repo))
    assert verdict["verdict"] == "PASS"  # the review still runs and still passes…
    assert verdict["signature"]["signed"] is False  # …it is simply not certifiable
    # The refusal is machine-readable POLICY, distinguishable from a signing *failure*
    # (which carries an `error` key) and from a non-PASS reason (the verdict string).
    assert verdict["signature"]["reason"] == "local-source-never-signs"
    # Nothing was written to the ticket either — the durable artifact is absent, not just the
    # in-verdict flag.
    assert rebar.show_ticket(tid, repo_root=str(rebar_repo)).get("signature") in (None, {})


def test_local_source_review_leaves_the_claim_gate_blocked(rebar_repo: Path) -> None:
    """The consequence that makes it a SECURITY property, not a cosmetic flag: a local review
    cannot buy a claim. This is the exact bypass that was reachable before the fix."""
    from rebar.llm.plan_review import attest

    _commit(rebar_repo)
    _enable(rebar_repo)
    tid = _make(rebar_repo)
    rebar.llm.review_plan(tid, runner=_CLEAN, source="local", repo_root=str(rebar_repo))
    assert attest.claim_gate_check(tid, repo_root=str(rebar_repo))["ok"] is False
    with pytest.raises(rebar.RebarError) as ei:
        rebar.claim(tid, assignee="me", repo_root=str(rebar_repo))
    assert ei.value.returncode == 1
    assert _status(tid, rebar_repo) == "open"


def test_no_signed_attestation_can_carry_a_null_verified_at_sha(rebar_repo: Path) -> None:
    """The invariant behind the rule: a signature asserts "this plan was reviewed against THIS
    committed tree". An unattested read has no committed tree to name, so any signed
    attestation must carry a real `verified-at-sha`. Guards against a future change that
    re-enables local signing without also pinning a SHA."""
    _commit(rebar_repo)
    _enable(rebar_repo)

    for label, kwargs in (
        ("attested", {}),
        ("local", {"source": "local"}),
    ):
        tid = _make(rebar_repo)
        verdict = rebar.llm.review_plan(tid, runner=_CLEAN, repo_root=str(rebar_repo), **kwargs)
        if not verdict["signature"]["signed"]:
            continue  # unsigned is the local case — nothing to constrain
        sha = verdict.get("verified_at_sha")
        assert isinstance(sha, str) and sha, f"{label}: a SIGNED verdict must name a commit"


def test_sign_review_refuses_to_resign_a_local_source_pass(rebar_repo: Path) -> None:
    """The completeness gap behind the never-sign guard: the recovery path (`rebar
    sign-review`) re-signs from the REVIEW_RESULT sidecar, so a local-source PASS sidecar
    must be refused too — otherwise a dirty-worktree review could still buy a claim via the
    back door. The sidecar records the resolved source precisely for this refusal."""
    _commit(rebar_repo)
    _enable(rebar_repo)
    tid = _make(rebar_repo)
    verdict = rebar.llm.review_plan(tid, runner=_CLEAN, source="local", repo_root=str(rebar_repo))
    assert verdict["verdict"] == "PASS" and verdict["signature"]["signed"] is False
    res = rebar.llm.resign_plan_review(tid, repo_root=str(rebar_repo))
    assert res["ok"] is False and res["signed"] is False
    assert "local" in res["reason"]
    with pytest.raises(rebar.RebarError):
        rebar.claim(tid, assignee="me", repo_root=str(rebar_repo))
    assert _status(tid, rebar_repo) == "open"


def test_sign_review_recovery_binds_a_committed_verified_at_sha(rebar_repo: Path) -> None:
    """The recovery path itself now signs inside an attested gate session, so the recovered
    attestation carries a real verified-at-sha pin (it used to sign from the working tree
    with no pin — violating the invariant the review path enforces)."""
    from rebar import signing

    _commit(rebar_repo)
    _enable(rebar_repo)
    tid = _make(rebar_repo)
    assert _review(tid, rebar_repo)["signature"]["signed"] is True
    res = rebar.llm.resign_plan_review(tid, repo_root=str(rebar_repo))
    assert res["ok"] is True, res
    v = rebar.verify_signature(tid, repo_root=str(rebar_repo))
    sha = signing.verified_at_sha_from_manifest(v["manifest"])
    assert isinstance(sha, str) and sha


def test_drift_refreshed_attestation_carries_a_verified_at_sha(rebar_repo: Path) -> None:
    """A drift refresh re-binds the prior verdict to current hashes; it must pin the
    committed tree those hashes came from, like any other new attestation (the refresh
    path used to omit the pin entirely)."""
    from rebar import signing

    _commit(rebar_repo)
    _enable(rebar_repo)
    tid = _scoped(rebar_repo)
    (rebar_repo / "dep.py").write_text("v = 1  # cosmetic edit; plan still holds\n")
    _commit_all(rebar_repo)
    v = _review(tid, rebar_repo)
    assert v["signature"].get("refreshed") is True and v["verdict"] == "PASS"
    ver = rebar.verify_signature(tid, repo_root=str(rebar_repo))
    sha = signing.verified_at_sha_from_manifest(ver["manifest"])
    assert isinstance(sha, str) and sha


# ── 3e4b saddened-unadult-snowmonkey: a container's drift scope inherits its direct
# children's declared file_impact (poison rule; closed children neither add nor poison) ──


def _container_with_children(repo: Path, impacts: list) -> tuple[str, list[str]]:
    """A story with one task child per entry of ``impacts`` (a file_impact list, or None
    to leave the child undeclared). Returns (parent_id, child_ids)."""
    parent = rebar.create_ticket("story", "container plan", description=_DESC, repo_root=str(repo))
    kids = []
    for i, impact in enumerate(impacts):
        kid = rebar.create_ticket(
            "task", f"child {i}", description=_DESC, parent=parent, repo_root=str(repo)
        )
        if impact:
            rebar.set_file_impact(kid, impact, repo_root=str(repo))
        kids.append(kid)
    return parent, kids


def _signed_deps(tid: str, repo: Path) -> dict:
    from rebar.llm.plan_review import attest

    v = rebar.verify_signature(tid, repo_root=str(repo))
    return attest.manifest_deps(v["manifest"])


def test_container_dep_set_inherits_the_children_union(rebar_repo: Path) -> None:
    (rebar_repo / "a.py").write_text("A = 1\n")
    (rebar_repo / "b.py").write_text("B = 1\n")
    _commit_all(rebar_repo)
    _enable(rebar_repo)
    parent, _kids = _container_with_children(
        rebar_repo,
        [[{"path": "a.py", "reason": "r"}], [{"path": "b.py", "reason": "r"}]],
    )
    assert _review(parent, rebar_repo)["signature"]["signed"] is True
    deps = _signed_deps(parent, rebar_repo)
    assert set(deps) >= {"a.py", "b.py"}, deps


def test_container_poison_rule_keeps_the_pre_change_set(rebar_repo: Path) -> None:
    """One non-closed child with NO file_impact → no inheritance at all: a partial union
    would be fail-open for exactly the undeclared scope. The empty set falls back to
    whole-HEAD invalidation (fail-closed), pinned by the claim being blocked after an
    UNRELATED commit."""
    (rebar_repo / "a.py").write_text("A = 1\n")
    _commit_all(rebar_repo)
    _enable(rebar_repo)
    parent, _kids = _container_with_children(rebar_repo, [[{"path": "a.py", "reason": "r"}], None])
    assert _review(parent, rebar_repo)["signature"]["signed"] is True
    assert _signed_deps(parent, rebar_repo) == {}
    (rebar_repo / "unrelated.py").write_text("U = 1\n")
    _commit_all(rebar_repo)  # whole-HEAD fallback: ANY landed commit invalidates
    with pytest.raises(rebar.RebarError):
        rebar.claim(parent, assignee="me", repo_root=str(rebar_repo))
    assert _status(parent, rebar_repo) == "open"


def test_container_closed_children_neither_contribute_nor_poison(rebar_repo: Path) -> None:
    (rebar_repo / "a.py").write_text("A = 1\n")
    _commit_all(rebar_repo)
    parent, kids = _container_with_children(rebar_repo, [[{"path": "a.py", "reason": "r"}], None])
    # Close the impact-less child BEFORE the gate is enabled (delivered work).
    rebar.claim(kids[1], assignee="me", repo_root=str(rebar_repo))
    rebar.transition(kids[1], "in_progress", "closed", repo_root=str(rebar_repo))
    _enable(rebar_repo)
    assert _review(parent, rebar_repo)["signature"]["signed"] is True
    deps = _signed_deps(parent, rebar_repo)
    assert "a.py" in deps  # the live child's scope is inherited…
    # …and the closed child's emptiness did not poison the union back to whole-HEAD.
    assert deps != {}


def test_container_unrelated_commit_does_not_invalidate(rebar_repo: Path) -> None:
    (rebar_repo / "a.py").write_text("A = 1\n")
    _commit_all(rebar_repo)
    _enable(rebar_repo)
    parent, _kids = _container_with_children(rebar_repo, [[{"path": "a.py", "reason": "r"}]])
    assert _review(parent, rebar_repo)["signature"]["signed"] is True
    (rebar_repo / "unrelated.py").write_text("U = 1\n")
    _commit_all(rebar_repo)
    rebar.claim(parent, assignee="me", repo_root=str(rebar_repo))  # scoped: claim survives
    assert _status(parent, rebar_repo) == "in_progress"


def test_container_child_declared_file_drift_invalidates(rebar_repo: Path) -> None:
    (rebar_repo / "a.py").write_text("A = 1\n")
    _commit_all(rebar_repo)
    _enable(rebar_repo)
    parent, _kids = _container_with_children(rebar_repo, [[{"path": "a.py", "reason": "r"}]])
    assert _review(parent, rebar_repo)["signature"]["signed"] is True
    (rebar_repo / "a.py").write_text("A = 2  # the child-declared file moved on main\n")
    _commit_all(rebar_repo)
    with pytest.raises(rebar.RebarError) as ei:
        rebar.claim(parent, assignee="me", repo_root=str(rebar_repo))
    assert "drift" in ei.value.stderr.lower()
    assert _status(parent, rebar_repo) == "open"


def test_child_impact_edit_invalidates_the_container_attestation(rebar_repo: Path) -> None:
    """The self-healing that makes signing-time inheritance sound: a child's file_impact
    is covered by its material fingerprint, which the container review PINS — so editing
    it invalidates the container attestation (forcing a re-review that recomputes the
    union) without any drift machinery involved. The pin check invalidates the CLAIM only
    under ``verify.enforce_plan_material_pins`` (schema default false; rebar's own project
    sets it true) — the config pairing the docs recommend alongside inheritance."""
    from rebar.llm.plan_review import attest

    (rebar_repo / "a.py").write_text("A = 1\n")
    _commit_all(rebar_repo)
    (rebar_repo / "rebar.toml").write_text(
        "[verify]\nrequire_plan_review_for_claim = true\nenforce_plan_material_pins = true\n"
    )
    parent, kids = _container_with_children(rebar_repo, [[{"path": "a.py", "reason": "r"}]])
    assert _review(parent, rebar_repo)["signature"]["signed"] is True
    assert attest.claim_gate_check(parent, repo_root=str(rebar_repo))["ok"] is True
    rebar.set_file_impact(
        kids[0], [{"path": "widened.py", "reason": "scope grew"}], repo_root=str(rebar_repo)
    )
    chk = attest.claim_gate_check(parent, repo_root=str(rebar_repo))
    assert chk["ok"] is False, chk
