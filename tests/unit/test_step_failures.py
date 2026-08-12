"""Repeated non-fatal LLM step failures must be visible in a gate run summary
(ticket eclectic-industrial-argali).

An LLM sub-step that fails is usually swallowed on purpose — the overlap judge treats a dead
batch as abstain, the novelty sub-calls degrade to un-floored — so the run continues and the
verdict never mentions it. Observed live: three overlap-judge batches, each burning ~310s,
each ending in ``abstain``; the emitted JSON was byte-identical to a run in which nothing
overlapped. These tests pin the tally that closes that gap and, just as importantly, pin that
a CLEAN run's coverage is unchanged (the count rides on a signed attestation).

The wiring test drives the REAL runner to failure rather than calling ``record`` by hand,
because the defect is about whether the runner's except spine reaches the sink at all.
Offline throughout: a ``FunctionModel`` with ``ALLOW_MODEL_REQUESTS = False``, the seam
``tests/unit/test_usage_log_failed_calls.py`` already uses.
"""

from __future__ import annotations

import io
from contextlib import redirect_stdout

import pytest

from rebar.llm import step_failures

pytestmark = pytest.mark.unit


# ── the sink itself ──────────────────────────────────────────────────────────────
def test_records_and_drains_per_step_counts():
    """The shape a caller consumes: a total plus a per-step breakdown, keyed by call label."""
    with step_failures.collect_step_failures():
        step_failures.record("overlap-judge")
        step_failures.record("overlap-judge")
        step_failures.record("overlap-judge")
        step_failures.record("verify")
        assert step_failures.drain() == {
            "total": 4,
            "by_step": {"overlap-judge": 3, "verify": 1},
        }


def test_drain_is_empty_when_nothing_failed():
    """The clean-run case — an EMPTY dict, which is what keeps the key off the verdict."""
    with step_failures.collect_step_failures():
        assert step_failures.drain() == {}


def test_record_outside_a_scope_is_a_silent_no_op():
    """Unit-testing a runner in isolation must neither raise nor leak a count into the next
    run that happens to open a scope."""
    step_failures.record("overlap-judge")  # no scope active
    assert step_failures.drain() == {}
    with step_failures.collect_step_failures():
        assert step_failures.drain() == {}


def test_drain_clears_so_a_second_run_starts_at_zero():
    """Destructive drain + scope exit: a count can never survive into a later run/ticket."""
    with step_failures.collect_step_failures():
        step_failures.record("overlap-judge")
        assert step_failures.drain()["total"] == 1
        assert step_failures.drain() == {}  # drained
    with step_failures.collect_step_failures():
        assert step_failures.drain() == {}  # a fresh run starts clean
    assert step_failures.drain() == {}  # and nothing survives outside a scope


def test_nested_scope_reuses_the_active_sink():
    """Nesting is idempotent (the contract-violation sink's posture): an inner scope must not
    shadow the run's sink and silently discard counts on exit."""
    with step_failures.collect_step_failures():
        with step_failures.collect_step_failures():
            step_failures.record("overlap-judge")
        assert step_failures.drain() == {"total": 1, "by_step": {"overlap-judge": 1}}


def test_empty_label_falls_back_to_the_question_mark_key():
    """The runner's own fallback when a call carries neither reviewers nor a ticket id."""
    with step_failures.collect_step_failures():
        step_failures.record("")
        assert step_failures.drain() == {"total": 1, "by_step": {"?": 1}}


# ── the verdict-assembly seam ────────────────────────────────────────────────────
def _coach_op():
    """The Pass-4 step that assembles the verdict's ``coverage``. Importing ``workflow_ops`` is
    what registers it, so ask for it only through here."""
    from rebar.llm.plan_review import workflow_ops  # noqa: F401 — import registers the steps
    from rebar.llm.workflow.executor import STEP_REGISTRY

    return STEP_REGISTRY["plan_review_coach"]


def _coach_ctx():
    from rebar.llm.workflow.executor import StepContext

    return StepContext(
        run_id="r",
        step_id="coach",
        kind="scripted",
        step={},
        inputs={
            "canonical_id": "0000-0000-0000-0001",
            "ticket_type": "task",
            "blocking": [],
            "surfaced": [],
            "overflow": [],
            "indeterminate": [],
            "dropped": [],
            "notes": [],
            "det_coverage": {},
            "routing": {},
        },
        workflow={},
        target_ticket="0000-0000-0000-0001",
        repo_root=None,
    )


def test_verdict_coverage_carries_the_tally_when_a_step_failed():
    op = _coach_op()
    with step_failures.collect_step_failures():
        step_failures.record("overlap-judge")
        step_failures.record("overlap-judge")
        out = op(_coach_ctx())
    assert out["coverage"]["llm_step_failures"] == {
        "total": 2,
        "by_step": {"overlap-judge": 2},
    }
    # Observability only: the tally must not have moved the verdict.
    assert out["verdict"] == "PASS"


def test_clean_run_coverage_omits_the_key_entirely():
    """Attestation safety: with nothing recorded the key is ABSENT, not an empty/zero object,
    so a clean run's coverage stays byte-identical to before this ticket."""
    op = _coach_op()
    with step_failures.collect_step_failures():
        out = op(_coach_ctx())
    assert "llm_step_failures" not in out["coverage"]
    # And with no sink active at all (a caller outside the gate's run scope).
    out_unscoped = _coach_op()(_coach_ctx())
    assert "llm_step_failures" not in out_unscoped["coverage"]


# ── the runner → sink wiring ─────────────────────────────────────────────────────
def test_a_failed_runner_call_reaches_the_sink_under_its_call_label():
    """The end-to-end claim: the REAL runner's except spine records the failure under the same
    label its ``llm call [<label>] ... FAILED`` log line uses, and the tally reaches the drain
    the verdict assembly consumes. Exercises the record site + the scope + the drain together,
    which the sink-only tests above cannot."""
    pytest.importorskip("pydantic_ai")
    import pydantic_ai.models
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    from rebar.llm.config import LLMConfig
    from rebar.llm.runner import PydanticAIRunner, RunRequest

    original = pydantic_ai.models.ALLOW_MODEL_REQUESTS
    pydantic_ai.models.ALLOW_MODEL_REQUESTS = False
    try:

        def blow_up(messages, info: AgentInfo):
            raise RuntimeError("the provided model identifier is invalid")

        cfg = LLMConfig(repo_path=".")
        req = RunRequest(
            system_prompt="s",
            instructions="i",
            config=cfg,
            reviewers=["overlap-judge"],
            mode="text",
        )
        with step_failures.collect_step_failures():
            with pytest.raises(Exception):  # noqa: B017 — the spine always re-raises
                PydanticAIRunner(cfg, model_override=FunctionModel(blow_up)).run(req)
            assert step_failures.drain() == {"total": 1, "by_step": {"overlap-judge": 1}}
    finally:
        pydantic_ai.models.ALLOW_MODEL_REQUESTS = original


# ── the text renderer ────────────────────────────────────────────────────────────
def _render(result: dict) -> str:
    from rebar._cli._llm_commands import _render_plan_review_text

    buf = io.StringIO()
    with redirect_stdout(buf):
        _render_plan_review_text(result)
    return buf.getvalue()


def test_text_output_names_the_total_and_the_per_step_counts():
    out = _render(
        {
            "verdict": "PASS",
            "ticket_id": "0000-0000-0000-0001",
            "coverage": {
                "llm_step_failures": {
                    "total": 4,
                    "by_step": {"overlap-judge": 3, "verify": 1},
                }
            },
        }
    )
    assert "llm step failures: 4" in out
    assert "overlap-judge=3" in out
    assert "verify=1" in out
    assert "non-fatal" in out


def test_text_output_is_silent_when_no_step_failed():
    out = _render({"verdict": "PASS", "ticket_id": "0000-0000-0000-0001", "coverage": {}})
    assert "llm step failures" not in out
