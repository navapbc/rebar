"""Transport-agnostic write-op parity contract — the single source of oracle cases.

This is the contract table for the Pattern-B conformance oracle in
``test_write_parity_oracle.py``. ONE table of rows, each executed through EVERY
adapter (library / CLI / MCP) against a fresh store and classified
``ACCEPTED`` / ``REJECTED(code)`` / ``PARAM_NOT_EXPOSED``. The oracle asserts
every adapter agrees with a row's transport-agnostic expectation; a per-adapter
strict-xfail records a KNOWN, ticketed divergence (the MCP force/reason/
caused_by/ref gaps tracked by ``scratchy-leprous-galago``), so the suite is
GREEN until the gap is closed — at which point the classification flips, the
xfail becomes an xpass, and the strict marker fails, forcing its removal.

The contract asserts BEHAVIOR — including the runtime conditional-required rules
(``close_class`` required to close a bug; a reason-required disposition refuses
without ``reason``) — which shape/signature comparison cannot express. Setup is
performed adapter-neutrally through the library; only the final op under test is
driven through the adapter, so a setup failure never masquerades as a parity
divergence.
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

# The scratchy-leprous-galago MCP gaps: transition_ticket/claim_ticket do not yet
# thread these reason-carrying params. Rows exercising them mark MCP strict-xfail.
SCRATCHY = "scratchy-leprous-galago"


@dataclass(frozen=True)
class Result:
    """A row's classification for one adapter.

    ``kind`` is ACCEPTED / REJECTED / PARAM_NOT_EXPOSED. ``code`` is the shared
    engine exit code on a REJECTED outcome (10 for optimistic-concurrency), so a
    rejection's identity — not merely its presence — is compared across adapters.
    """

    kind: str
    code: int | None = None


@dataclass(frozen=True)
class Case:
    """One transport-agnostic contract row.

    ``op`` is the write op under test. ``setup_type`` is the ticket type created
    for the subject; ``pre_in_progress`` moves it to in_progress first (via the
    library, gate-free) so a close/close-gate row starts from a real work state.
    ``gate`` enables the claim or close gate for the row so a force-bypass row is
    non-vacuous. ``inputs`` are the reason-carrying params threaded to the op.
    ``expected`` is what a fully-parity surface must do; ``expected_status`` is
    the post-op effect asserted on ACCEPTED. ``xfail`` maps an adapter name to
    the ticket whose landing will close its known divergence.
    """

    id: str
    op: str  # "create" | "claim" | "transition"
    expected: Result
    setup_type: str = "task"
    pre_in_progress: bool = False
    gate: str | None = None  # None | "claim" | "close"
    target: str = "closed"
    inputs: dict = field(default_factory=dict)
    needs_culprit: bool = False
    expected_status: str | None = None
    xfail: dict = field(default_factory=dict)


# A plan body that clears the plan-review readiness floor, so a claim/start-work
# gate blocks on the ATTESTATION (which --force bypasses) rather than on missing
# plan structure.
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
    """Give the CODE branch a commit so a gate's ref=HEAD snapshot resolves."""
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
    # Positive control: create is at parity on every surface.
    Case(id="create-baseline", op="create", expected=Result(ACCEPTED)),
    # Negative control: an optimistic-concurrency rejection shares ONE identity
    # (exit 10) across all three surfaces — proves the harness detects agreement
    # on a rejection, not just on acceptance.
    Case(
        id="concurrency-wrong-current",
        op="transition",
        target="closed",
        inputs={},
        expected=Result(REJECTED, code=10),
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
    ),
    # ── force parity (MCP gap → strict-xfail citing scratchy) ─────────────────
    Case(
        id="force-transition-close",
        op="transition",
        pre_in_progress=True,
        gate="close",
        inputs={"force": "oracle bypass"},
        expected=Result(ACCEPTED),
        expected_status="closed",
        xfail={"mcp": SCRATCHY},
    ),
    Case(
        id="force-claim",
        op="claim",
        gate="claim",
        inputs={"force": "oracle bypass"},
        expected=Result(ACCEPTED),
        expected_status="in_progress",
        xfail={"mcp": SCRATCHY},
    ),
    # ── reason as close_reason (MCP gap → strict-xfail citing scratchy) ────────
    Case(
        id="reason-close-obsolete",
        op="transition",
        pre_in_progress=True,
        inputs={"close_class": "obsolete", "reason": "no longer needed"},
        expected=Result(ACCEPTED),
        expected_status="closed",
        xfail={"mcp": SCRATCHY},
    ),
    # ── caused_by on a bug close (MCP gap → strict-xfail citing scratchy) ──────
    Case(
        id="caused-by-bug-close",
        op="transition",
        setup_type="bug",
        pre_in_progress=True,
        inputs={"close_class": "regression"},
        needs_culprit=True,
        expected=Result(ACCEPTED),
        expected_status="closed",
        xfail={"mcp": SCRATCHY},
    ),
    # ── ref on a close (MCP gap → strict-xfail citing scratchy) ────────────────
    Case(
        id="ref-close",
        op="transition",
        pre_in_progress=True,
        inputs={"ref": "HEAD"},
        expected=Result(ACCEPTED),
        expected_status="closed",
        xfail={"mcp": SCRATCHY},
    ),
]


# ── Execution ─────────────────────────────────────────────────────────────────
def _to_result(outcome) -> Result:
    """Map an adapter Outcome to the contract classification."""
    if outcome.ok:
        return Result(ACCEPTED)
    if outcome.is_param_gap:
        return Result(PARAM_NOT_EXPOSED)
    return Result(REJECTED, code=outcome.code)


def execute(adapter, case: Case, repo: Path) -> tuple[Result, str | None]:
    """Run one case through one adapter against ``repo``; return (result, subject).

    Setup (ticket creation, the pre-work move, a culprit ticket) goes through the
    library so it is adapter-neutral; only the op under test is driven through
    ``adapter``. The returned subject id lets the oracle assert the ACCEPTED
    effect (resulting status).
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

    if case.op == "claim":
        outcome = adapter.claim(tid, **inputs)
    else:
        # Every transition case drives current="in_progress": the pre_in_progress
        # rows are genuinely there, while the concurrency control leaves the
        # subject OPEN so "in_progress" is deliberately the wrong current status,
        # yielding the shared optimistic-concurrency rejection (exit 10).
        outcome = adapter.transition(tid, "in_progress", case.target, **inputs)
    return _to_result(outcome), tid
