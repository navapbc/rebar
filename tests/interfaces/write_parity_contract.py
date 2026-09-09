"""Transport-neutral write parity contract for the shared conformance oracle.

Every row runs through library, CLI, and MCP against a fresh store and must
match one ``ACCEPTED`` / ``REJECTED(code)`` / ``PARAM_NOT_EXPOSED`` result. A
populated strict xfail marks a ticketed surface gap and fails on convergence.
Rows cover runtime rules that signature comparison cannot. Adapter-neutral setup
isolates the final operation.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import rebar

# ── Classification ────────────────────────────────────────────────────────────
ACCEPTED = "ACCEPTED"
REJECTED = "REJECTED"
PARAM_NOT_EXPOSED = "PARAM_NOT_EXPOSED"

# Adapter-bound plumbing is outside the contract. The create baseline proves
# these surface-specific arguments do not cause false divergence.
ADAPTER_BOUND_INTERNALS = ("source", "return_alias", "_creation_channel", "repo_root")


@dataclass(frozen=True)
class Result:
    """One adapter classification.

    ``kind`` is ACCEPTED, REJECTED, or PARAM_NOT_EXPOSED. ``code`` identifies a
    rejected engine outcome across surfaces.
    """

    kind: str
    code: int | None = None


@dataclass(frozen=True)
class Case:
    """One transport-neutral operation case.

    ``op`` names the final write. ``setup_type`` and ``pre_in_progress`` define
    its subject. ``gate`` makes bypass cases non-vacuous. ``inputs`` reach the
    operation, ``expected`` and ``expected_status`` describe accepted effects,
    and ``unmutated_status`` proves rejection is atomic. ``xfail`` maps a surface
    to its divergence ticket.
    """

    id: str
    op: str  # "create" | "claim" | "transition" | "link"
    expected: Result
    setup_type: str = "task"
    pre_in_progress: bool = False
    gate: str | None = None  # None | "claim" | "close"
    target: str = "closed"
    inputs: dict = field(default_factory=dict)
    needs_culprit: bool = False
    expected_status: str | None = None
    unmutated_status: str | None = None
    xfail: dict = field(default_factory=dict)


# Keep claim-gate subjects structurally reviewable so the attestation, not plan
# readiness, is what ``--force`` bypasses.
_DESC = (
    "A sufficiently detailed plan body for the parity oracle subject.\n\n"
    "## Approach\nDo the thing carefully.\n\n"
    "## Scope\nsrc/x.py\n\n"
    "## Testing\n`pytest -q`\n\n"
    "## Acceptance Criteria\n- [ ] the thing works (checked: `pytest -q`)\n"
)

_GATE_KEY = {
    "claim": "require_plan_review_for_claim",
    "close": "require_completion_verification_for_close",
}


def _commit(repo: Path) -> None:
    """Seed CODE so a ``ref=HEAD`` gate snapshot resolves."""
    subprocess.run(
        ["git", "commit", "--allow-empty", "-q", "-m", "oracle"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )


def _enable_gate(repo: Path, gate: str) -> None:
    (repo / "rebar.toml").write_text(f"[verify]\n{_GATE_KEY[gate]} = true\n")


# ── The contract table ────────────────────────────────────────────────────────
CASES: list[Case] = [
    # Create proves acceptance and that adapter-bound plumbing adds no false drift.
    Case(id="create-baseline", op="create", expected=Result(ACCEPTED)),
    # Leave the subject open but request current="in_progress". Every surface must
    # return exit 10 without changing it.
    Case(
        id="concurrency-wrong-current",
        op="transition",
        target="closed",
        inputs={},
        expected=Result(REJECTED, code=10),
        unmutated_status="open",
    ),
    # close_class parity: exposed on library, CLI (--class) AND MCP — a valid bug
    # close under a non-reason-required class succeeds identically everywhere.
    Case(
        id="close-class-valid-bug",
        op="transition",
        setup_type="bug",
        pre_in_progress=True,
        inputs={"close_class": "regression"},
        expected=Result(ACCEPTED),
        expected_status="closed",
    ),
    # Runtime conditional rule: closing a bug with NO class is refused — the same
    # refusal on all three surfaces (close_class is universally exposed).
    Case(
        id="close-class-missing-bug",
        op="transition",
        setup_type="bug",
        pre_in_progress=True,
        inputs={},
        expected=Result(REJECTED, code=1),
        unmutated_status="in_progress",
    ),
    # Runtime conditional rule: a reason-required disposition (obsolete) refuses
    # without a reason — identically on all three (reason is not even supplied).
    Case(
        id="reason-required-missing-bug",
        op="transition",
        setup_type="bug",
        pre_in_progress=True,
        inputs={"close_class": "obsolete"},
        expected=Result(REJECTED, code=1),
        unmutated_status="in_progress",
    ),
    # ── force parity ─────────────────────────────────────────────────────────
    Case(
        id="force-transition-close",
        op="transition",
        pre_in_progress=True,
        gate="close",
        inputs={"force": "oracle bypass"},
        expected=Result(ACCEPTED),
        expected_status="closed",
    ),
    Case(
        id="force-claim",
        op="claim",
        gate="claim",
        inputs={"force": "oracle bypass"},
        expected=Result(ACCEPTED),
        expected_status="in_progress",
    ),
    # ── reason as close_reason (converged on all three surfaces) ──────────────
    Case(
        id="reason-close-obsolete",
        op="transition",
        pre_in_progress=True,
        inputs={"close_class": "obsolete", "reason": "no longer needed"},
        expected=Result(ACCEPTED),
        expected_status="closed",
    ),
    # ── caused_by on a bug close ─────────────────────────────────────────────
    Case(
        id="caused-by-bug-close",
        op="transition",
        setup_type="bug",
        pre_in_progress=True,
        inputs={"close_class": "regression"},
        needs_culprit=True,
        expected=Result(ACCEPTED),
        expected_status="closed",
    ),
    # ── caused_by link-time validation ───────────────────────────────────────
    # A commitless caused_by target is rejected everywhere. Its reasoned bypass
    # must also agree across surfaces.
    Case(
        id="caused-by-commitless-target",
        op="link",
        expected=Result(REJECTED, code=1),
        unmutated_status="open",
    ),
    Case(
        id="caused-by-force-bypass",
        op="link",
        inputs={"force": "oracle bypass"},
        expected=Result(ACCEPTED),
        expected_status="open",
    ),
    # ── ref on a close ───────────────────────────────────────────────────────
    Case(
        id="ref-close",
        op="transition",
        pre_in_progress=True,
        inputs={"ref": "HEAD"},
        expected=Result(ACCEPTED),
        expected_status="closed",
    ),
]


# ── Execution ─────────────────────────────────────────────────────────────────
def _to_result(outcome) -> Result:
    """Classify an adapter outcome."""
    if outcome.ok:
        return Result(ACCEPTED)
    if outcome.is_param_gap:
        return Result(PARAM_NOT_EXPOSED)
    return Result(REJECTED, code=outcome.code)


def execute(adapter, case: Case, repo: Path) -> tuple[Result, str | None]:
    """Run a case through one adapter and return its result and subject.

    Library-only setup isolates the final surface operation. The subject lets
    callers verify accepted effects.
    """
    _commit(repo)
    if case.gate:
        _enable_gate(repo, case.gate)

    if case.op == "create":
        try:
            tid = adapter.create(case.setup_type, "oracle create", description=_DESC)
        except Exception:  # noqa: BLE001 — create parity failure is a divergence, surfaced as REJECTED
            return Result(REJECTED), None
        return Result(ACCEPTED), tid

    tid = rebar.create_ticket(
        case.setup_type, "oracle subject", description=_DESC, repo_root=str(repo)
    )
    if case.pre_in_progress:
        rebar.transition(tid, "open", "in_progress", repo_root=str(repo))

    inputs = dict(case.inputs)
    if case.needs_culprit:
        inputs["caused_by"] = rebar.create_ticket(
            "task", "oracle culprit", description=_DESC, repo_root=str(repo)
        )

    if case.op == "link":
        target = rebar.create_ticket(
            "task", "oracle link target", description=_DESC, repo_root=str(repo)
        )
        outcome = adapter.link(tid, target, "caused_by", **inputs)
    elif case.op == "claim":
        outcome = adapter.claim(tid, **inputs)
    else:
        # Prepared rows are in progress. The open control must reject this current
        # value with exit 10.
        outcome = adapter.transition(tid, "in_progress", case.target, **inputs)
    return _to_result(outcome), tid
