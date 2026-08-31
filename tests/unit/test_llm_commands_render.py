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
        "overlap": [],
        "signature": {"signed": True},
    }
    out = _captured(_render_plan_review_text, result)
    assert "sev=" not in out
    assert "tighten wording" in out
