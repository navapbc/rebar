"""Rendering tests for the review/plan-review CLI text renderers (rebar._cli._llm_commands).

Focused on the severity -> priority/decision migration (epic pink-complex-xenurine): the
human-facing text shows blocking/advisory, not a severity word, for producers that carry a
`decision` field -- but `_render_review_text` is shared with the severity-only `scan-spec`
path, which must keep its severity fallback unchanged.
"""

from __future__ import annotations

import contextlib
import io

from rebar._cli._llm_commands import _render_plan_review_text, _render_review_text


def _captured(fn, arg):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(arg)
    return buf.getvalue()


def test_render_review_text_shows_blocking_advisory_for_decision_bearing_findings():
    """A code-review finding (decision present) prints BLOCKING/ADVISORY, not severity."""
    result = {
        "runner": "fake",
        "model": None,
        "target": {"ticket_ids": ["t1"]},
        "findings": [
            {"decision": "block", "severity": "critical", "dimension": "security", "detail": "d1"},
            {"decision": "advisory", "severity": "info", "dimension": "style", "detail": "d2"},
        ],
    }
    out = _captured(_render_review_text, result)
    assert "[BLOCKING]" in out
    assert "[ADVISORY]" in out
    assert "CRITICAL" not in out and "INFO" not in out


def test_render_review_text_keeps_severity_fallback_for_decisionless_spec_scan_findings():
    """A scan-spec finding (no decision field) keeps printing its severity word unchanged --
    this is the only signal that population has."""
    result = {
        "runner": "fake",
        "model": None,
        "target": {"ticket_ids": ["t1"]},
        "findings": [
            {"severity": "high", "dimension": "spec-alignment", "detail": "gap found"},
        ],
    }
    out = _captured(_render_review_text, result)
    assert "[HIGH]" in out
    assert "BLOCKING" not in out and "ADVISORY" not in out


def test_render_plan_review_text_advisory_line_has_no_severity_suffix():
    result = {
        "verdict": "PASS",
        "coverage": {},
        "blocking": [],
        "advisory": [
            {"criteria": ["ac-quality"], "severity": "minor", "finding": "tighten wording"},
        ],
        "indeterminate": [],
        "coaching": [],
        "signature": {"signed": True},
    }
    out = _captured(_render_plan_review_text, result)
    assert "sev=" not in out
    assert "tighten wording" in out


_DRIFT_REMEDY = (
    "this rebar build (aa1681bb5) is 2 commit(s) behind the config this gate read (37d8a50ab); "
    "update the checkout running the gate to that ref (git fetch origin && git merge --ff-only "
    "origin/main) and run `make install`, then re-run the gate"
)


def _degraded_plan_review(**coverage) -> dict:
    return {
        "verdict": "INDETERMINATE",
        "ticket_id": "t1",
        "coverage": coverage,
        "blocking": [],
        "advisory": [],
        "indeterminate": [],
        "coaching": [],
        "signature": {},
    }


def test_render_plan_review_text_names_build_drift_and_provider_error_when_model_never_ran():
    """Bug stupid-evasive-marten: a model-never-ran verdict's text report carries the provider
    error and the build-drift remedy that JSON already shows."""
    result = _degraded_plan_review(
        llm_ran=False,
        llm_error="the LLM provider call failed: The provided model identifier is invalid.",
        build_drift={"build_sha": "a" * 40, "pinned_sha": "b" * 40, "remedy": _DRIFT_REMEDY},
    )
    out = _captured(_render_plan_review_text, result)
    assert "model did not run: " in out
    assert "The provided model identifier is invalid." in out
    assert f"build drift: {_DRIFT_REMEDY}" in out
    assert "make install" in out


def test_render_plan_review_text_names_provider_error_without_build_drift():
    result = _degraded_plan_review(llm_ran=False, llm_error="the LLM provider call failed: 403")
    out = _captured(_render_plan_review_text, result)
    assert "model did not run: the LLM provider call failed: 403" in out
    assert "build drift:" not in out


def test_render_plan_review_text_clean_run_prints_no_failure_lines():
    for coverage in ({}, {"llm_ran": True, "llm_error": None}):
        out = _captured(_render_plan_review_text, _degraded_plan_review(**coverage))
        assert "model did not run" not in out
        assert "build drift" not in out
