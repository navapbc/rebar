"""Held-out oracle for bug b39a: when the completion verifier has already banked an
operator-actionable BLOCK and THEN hits a provider outage, the gate must finalize a
deterministic BLOCK verdict FROM THE BANK — with no further LLM calls — instead of dying
verdict-less. Honors the module invariant "a run with any banked progress can never die
verdict-less" (completion_recovery.py:11-12) under the OPERATOR DECISION re-scope: the trigger
is "the bank already holds an operator-actionable BLOCK," NOT "the outage is sustained."

An EMPTY bank — or a bank holding only PASSes / insufficiency placeholders — has nothing
actionable to surface, so it keeps the retryable exit-11 posture (ADR 0040): the outage
re-raises with its disposition intact.

Offline — no billable call. The fake primary run banks a genuine refutation through the
record tool it is handed (exactly as a real primary banks incrementally), then raises a
retryable ``LLMUnavailableError``. Mirrors ``test_completion_outage_disposition_8c8a.py``.
"""

from __future__ import annotations

import pytest

import rebar._reads as _reads_mod
from rebar.llm.config import LLMConfig
from rebar.llm.errors import LLMUnavailableError
from rebar.llm.failure import LLMOutcome, ResolutionClass
from rebar.llm.workflow import completion_banking as _bank
from rebar.llm.workflow import completion_recovery as _cr
from rebar.llm.workflow.completion_recovery import CompletionAgentStep
from rebar.llm.workflow.executor import StepContext

pytestmark = pytest.mark.unit

_TICKET_ID = "T-degrade"
_CRITERION = "The login endpoint rejects an expired token with HTTP 401."


def _retryable_outcome() -> LLMOutcome:
    return LLMOutcome(
        ResolutionClass.WAIT_AND_RETRY, {"type": "overload", "status_code": 529}, retryable=True
    )


def _fake_ticket() -> dict:
    return {
        "ticket_id": _TICKET_ID,
        "title": "degrade under outage",
        "ticket_type": "bug",
        "description": (
            "## Acceptance Criteria\n"
            f"- [ ] {_CRITERION}\n"
            "- [ ] A second criterion that was never reached.\n"
        ),
    }


def _expected_ids() -> dict[str, str]:
    from rebar.llm.workflow.completion_criteria import explicit_completion_criteria

    return _bank.criterion_id_map(explicit_completion_criteria(_fake_ticket()))


class _BankingOutageRunner:
    """A primary run that BANKS a verdict through its record tool, then dies with a retryable
    provider outage — the real incremental-banking-then-outage sequence.

    ``bank_action`` decides what it banks (an actionable BLOCK, a PASS, an insufficiency, or
    nothing) before raising, so one runner drives every scenario."""

    name = "outage"

    def __init__(self, bank_action) -> None:
        self._bank_action = bank_action
        self.calls = 0

    def preflight(self) -> None:
        return None

    def run(self, req):
        self.calls += 1
        record_tool = (req.extra_tools or [None])[0]
        if self._bank_action is not None and record_tool is not None:
            self._bank_action(record_tool)
        exc = LLMUnavailableError("the LLM provider call failed: read timed out")
        exc.outcome = _retryable_outcome()  # type: ignore[attr-defined]
        raise exc


def _ctx(tmp_path) -> StepContext:
    return StepContext(
        run_id="run-degrade",
        step_id="verify",
        kind="agent",
        step={
            "id": "verify",
            "prompt": "completion-verifier",
            "mode": "structured",
            "output_schema": "completion_verdict",
        },
        inputs={
            "ticket_id": _TICKET_ID,
            "context": "<untrusted_ticket_context>t</untrusted_ticket_context>",
        },
        workflow={"name": "completion-verification"},
        target_ticket=_TICKET_ID,
        repo_root=str(tmp_path),
    )


@pytest.fixture(autouse=True)
def _patch_ticket_read(monkeypatch):
    """Both the primary manifest read and the degrade's expected-criteria read resolve the
    fake ticket, so the deterministic verdict has real criteria to key against."""
    monkeypatch.setattr(_reads_mod, "show_ticket", lambda *a, **k: _fake_ticket())


def _run(tmp_path, bank_action):
    runner = _BankingOutageRunner(bank_action)
    step = CompletionAgentStep(
        runner=runner, repo_root=str(tmp_path), config=LLMConfig(runner="fake")
    )
    return step, runner


# ── the fix: an actionable banked BLOCK + outage degrades to a deterministic BLOCK ─────────
def test_actionable_bank_outage_degrades_to_deterministic_block(tmp_path) -> None:
    """A banked refutation (met=false, NOT insufficiency) + a provider outage must finalize a
    deterministic BLOCK from the bank with NO further LLM calls: verdict FAIL, certifiable
    False, finalizer deterministic_fallback. RED before the fix (the arm re-raised the outage
    unconditionally → LLMUnavailableError, verdict-less exit 11)."""
    cid = _expected_ids()[_CRITERION]

    def bank_block(record_tool):
        record_tool(cid, False, "GET /login with an expired token returned 200, not 401.")

    step, runner = _run(tmp_path, bank_block)
    result = step.run(_ctx(tmp_path))

    outputs = result.outputs
    assert outputs["verdict"] == "FAIL"
    assert outputs["certifiable"] is False
    assert outputs["finalizer"] == "deterministic_fallback"
    # AC1: NO additional LLM calls after the outage — only the single primary run.
    assert runner.calls == 1
    # The banked refutation is surfaced as an operator-actionable finding.
    assert any(f.get("criterion") == _CRITERION for f in outputs["findings"])


# ── the non-actionable paths keep the retryable exit-11 posture (re-raise) ─────────────────
def test_empty_bank_outage_reraises_exit11(tmp_path) -> None:
    """An EMPTY bank has nothing actionable to surface: the outage re-raises (exit-11 posture),
    and its retryable disposition is stamped for close_precheck to forward."""
    step, _ = _run(tmp_path, bank_action=None)
    with pytest.raises(LLMUnavailableError):
        step.run(_ctx(tmp_path))
    assert step.failure_diagnostic is not None
    assert step.failure_diagnostic.get("retryable") is True
    assert step.failure_diagnostic.get("resolution_class") == ResolutionClass.WAIT_AND_RETRY.value


def test_pass_only_bank_outage_reraises_exit11(tmp_path) -> None:
    """A bank holding only a PASS (met=true) is not operator-actionable — a certified PASS is
    unreachable without execution — so the outage re-raises rather than fabricating a verdict."""
    cid = _expected_ids()[_CRITERION]

    def bank_pass(record_tool):
        record_tool(cid, True, "expired token was rejected with 401.")

    step, _ = _run(tmp_path, bank_pass)
    with pytest.raises(LLMUnavailableError):
        step.run(_ctx(tmp_path))


def test_insufficiency_only_bank_outage_reraises_exit11(tmp_path) -> None:
    """A bank holding only an INSUFFICIENCY record (met=false + evidence_sufficient=false) is an
    evidence gap, not a refutation — nothing actionable. The model-facing record tool cannot
    write that framework-only marker, so assert the predicate directly."""
    entries = {
        "c00-abc": {"met": False, "evidence_sufficient": False},
    }
    assert _bank.bank_has_actionable_block(entries) is False
    # A PASS-plus-insufficiency mix is still non-actionable.
    entries["c01-def"] = {"met": True}
    assert _bank.bank_has_actionable_block(entries) is False
    # A genuine refutation flips it to actionable.
    entries["c02-ghi"] = {"met": False}
    assert _bank.bank_has_actionable_block(entries) is True


# ── the guarded degrade never becomes a NEW failure mode on the outage path ────────────────
def test_actionable_bank_but_no_expected_criteria_reraises(tmp_path, monkeypatch) -> None:
    """An actionable BLOCK is banked, but the ticket exposes NO explicit completion criteria to
    key a full-coverage verdict against: the degrade declines (no criteria to assemble over)
    and the outage re-raises. Covers the ``if not expected: return None`` branch."""
    cid = _expected_ids()[_CRITERION]
    monkeypatch.setattr(_cr, "explicit_completion_criteria", lambda *a, **k: [])

    def bank_block(record_tool):
        record_tool(cid, False, "refuted.")

    step, _ = _run(tmp_path, bank_block)
    with pytest.raises(LLMUnavailableError):
        step.run(_ctx(tmp_path))


def test_degrade_fault_falls_through_to_reraise(tmp_path, monkeypatch) -> None:
    """A fault while assembling the degrade verdict must NOT become a new failure mode: the
    guard swallows it and the outage re-raises unchanged (exit-11), disposition intact. Covers
    the best-effort ``except Exception`` fall-through arm."""
    cid = _expected_ids()[_CRITERION]

    def boom(*a, **k):
        raise RuntimeError("assembler blew up")

    monkeypatch.setattr(_bank, "assemble_deterministic_verdict", boom)

    def bank_block(record_tool):
        record_tool(cid, False, "refuted.")

    step, _ = _run(tmp_path, bank_block)
    with pytest.raises(LLMUnavailableError):
        step.run(_ctx(tmp_path))
    assert step.failure_diagnostic is not None
    assert step.failure_diagnostic.get("retryable") is True
