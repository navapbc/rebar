"""Disposition plumbing (story authorial-hated-blackbear, epic jira-reb-687): the
resolution-class + sanitized diagnostic flowing from an ``LLMOutcome`` through the degrade
verdicts, the never-sign guard, the code-review translation, and the MCP failure surface.
Offline — no billable call.
"""

from __future__ import annotations

import pytest

from rebar.llm.errors import LLMUnavailableError
from rebar.llm.failure import (
    LLMOutcome,
    ResolutionClass,
    log_degrade,
    message_for,
    outcome_of,
    resolution_fields,
)

pytestmark = pytest.mark.unit


def _retryable_error() -> LLMUnavailableError:
    exc = LLMUnavailableError("overloaded")
    exc.outcome = LLMOutcome(  # type: ignore[attr-defined]
        ResolutionClass.WAIT_AND_RETRY, {"type": "overload", "status_code": 529}, retryable=True
    )
    return exc


# ── helpers ───────────────────────────────────────────────────────────────────
def test_resolution_fields_from_outcome():
    o = LLMOutcome(ResolutionClass.RETRY_NOW, {"m": "x"}, retryable=True)
    assert resolution_fields(o) == {
        "resolution_class": "RETRY_NOW",
        "retryable": True,
        "diagnostic": {"m": "x"},
    }


def test_resolution_fields_none_is_empty():
    """No outcome (a string-error degrade path) contributes NOTHING — the verdict stays a
    plain INDETERMINATE with no disposition."""
    assert resolution_fields(None) == {}


def test_message_for_every_class_and_unknown():
    for rc in ResolutionClass:
        assert message_for(rc.value)  # every class has a human message
    assert message_for(None) is None
    assert message_for("NOT_A_CLASS") is None


def test_outcome_of_reads_attached_outcome():
    assert outcome_of(_retryable_error()).retryable is True
    assert outcome_of(LLMUnavailableError("bare")) is None
    assert outcome_of("a plain string error") is None


def test_log_degrade_never_raises(monkeypatch):
    """Best-effort: even if append_session_log blows up, the degrade path must not fail."""
    import rebar

    monkeypatch.setattr(
        rebar, "append_session_log", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    log_degrade(_retryable_error().outcome, gate="plan-review", ticket_id="t")  # no raise
    log_degrade(None, gate="plan-review")  # no-op, no raise


# ── shape-A write-site (code-review degrade helper — no ctx needed) ───────────
def test_degraded_code_review_carries_disposition():
    from rebar.llm.workflow.gate_dispatch import _degraded_code_review_verdict

    v = _degraded_code_review_verdict(error=_retryable_error(), runner_name="r")
    assert v["verdict"] == "INDETERMINATE"
    cov = v["coverage"]
    assert cov["resolution_class"] == "WAIT_AND_RETRY"
    assert cov["retryable"] is True
    assert cov["diagnostic"] == {"type": "overload", "status_code": 529}


def test_degraded_code_review_string_error_has_no_disposition():
    from rebar.llm.workflow.gate_dispatch import _degraded_code_review_verdict

    v = _degraded_code_review_verdict(error="finders produced nothing", runner_name="r")
    assert "resolution_class" not in v["coverage"]
    assert "retryable" not in v["coverage"]


# ── never-sign structural guard ───────────────────────────────────────────────
def test_sign_plan_review_refuses_non_pass():
    from rebar.llm.plan_review.attest import sign_plan_review
    from rebar.signing import SigningError

    with pytest.raises(SigningError, match="non-PASS / degraded"):
        sign_plan_review({"verdict": "INDETERMINATE", "coverage": {}}, material="m")


def test_sign_plan_review_refuses_degraded_pass():
    """Even a (nonsensical) PASS that carries a systemic-degrade disposition must not sign."""
    from rebar.llm.plan_review.attest import sign_plan_review
    from rebar.signing import SigningError

    with pytest.raises(SigningError, match="non-PASS / degraded"):
        sign_plan_review(
            {"verdict": "PASS", "coverage": {"resolution_class": "WAIT_AND_RETRY"}}, material="m"
        )


# ── code-review translation preserves coverage to the top level ───────────────
def test_verdict_to_review_result_preserves_coverage():
    from rebar.llm.code_review.shim import _verdict_to_review_result

    gate_verdict = {
        "verdict": "INDETERMINATE",
        "blocking": [],
        "advisory": [],
        "coverage": {"resolution_class": "RETRY_NOW", "retryable": True, "diagnostic": {"m": 1}},
    }
    rr = _verdict_to_review_result(gate_verdict, base="a", head="b", changed_files=[])
    assert rr["coverage"]["resolution_class"] == "RETRY_NOW"
    assert rr["coverage"]["retryable"] is True


# ── MCP structured failure return ─────────────────────────────────────────────
def test_structured_llm_failure_carries_disposition():
    from rebar._mcp_llm import _structured_llm_failure

    out = _structured_llm_failure(_retryable_error())
    assert out["retryable"] is True
    assert out["resolution_class"] == "WAIT_AND_RETRY"
    assert out["diagnostic"] == {"type": "overload", "status_code": 529}
    assert "overloaded" in out["error"]


def test_structured_llm_failure_bare_error():
    from rebar._mcp_llm import _structured_llm_failure

    out = _structured_llm_failure(LLMUnavailableError("no outcome"))
    assert out["retryable"] is False
    assert out["resolution_class"] is None


# ── pre-sign material fingerprint recheck ─────────────────────────────────────
def test_material_drifted_detects_moved_head():
    """Two real, DIFFERENT full SHAs → drift (HEAD moved between verify and sign → skip signing)."""
    from rebar._commands.transition_close import _material_drifted

    a = "a" * 40
    b = "b" * 40
    assert _material_drifted(a, b) is True
    assert _material_drifted(a, a) is False  # stable HEAD → sign


def test_material_drifted_ignores_non_sha_markers():
    """A synthetic/non-SHA verified marker (unattested/local verdict) is NOT comparable → NOT
    drift, so the normal sign-on-PASS path is preserved (guards against false 'unsigned')."""
    from rebar._commands.transition_close import _material_drifted

    real = "c" * 40
    assert _material_drifted("pinnedsha", real) is False
    assert _material_drifted(None, real) is False
    assert _material_drifted(real, None) is False
    assert _material_drifted("", "") is False
