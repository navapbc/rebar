"""WS4 (epic b744): produce_code_review_verdict + the gate-backed review_code shim + the
off-by-default flag + source-separation. Pins: inert/zero-LLM when disabled, a schema-valid
verdict when enabled, INDETERMINATE on outage, the verdict→review_result translation, the
sidecar on a target ticket, and that the single-pass route is retired.
"""

from __future__ import annotations

import pytest

from rebar import schemas
from rebar.llm.config import LLMConfig
from rebar.llm.errors import LLMUnavailableError
from rebar.llm.runner import FakeRunner
from rebar.llm.workflow import gate_dispatch

pytestmark = pytest.mark.unit

_DIFF = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n+print('hi')\n"
# A structured payload a FakeRunner returns for EVERY structured agent call (base/overlay/
# verify/coach) — enough to drive the gate offline to a verdict.
_STRUCTURED = {
    "findings": [],
    "recommend_overlays": [],
    "verifications": [],
    "notes": [],
    "summary": "x",
}


class _CountingRunner(FakeRunner):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.calls = 0

    def run(self, req):
        self.calls += 1
        return super().run(req)


class _OutageRunner(FakeRunner):
    def preflight(self) -> None:
        raise LLMUnavailableError("no agents extra / no key")


# ── disabled (default) → INERT, zero LLM ────────────────────────────────────────────────────
def test_produce_verdict_disabled_is_inert_and_makes_zero_llm_calls(monkeypatch):
    monkeypatch.setattr(gate_dispatch, "code_review_enabled", lambda repo_root=None: False)
    runner = _CountingRunner(structured=_STRUCTURED)
    v = gate_dispatch.produce_code_review_verdict(
        gate_dispatch.CodeReviewRequest(LLMConfig.from_env(), diff_text=_DIFF, runner=runner)
    )
    assert v["verdict"] == "PASS"
    assert v["blocking"] == [] and v["advisory"] == []
    assert v["coverage"]["enabled"] is False and v["coverage"]["llm_ran"] is False
    assert runner.calls == 0  # INERT: never ran the workflow / a model call


def test_review_code_disabled_returns_empty_review_result(monkeypatch):
    monkeypatch.setattr(gate_dispatch, "code_review_enabled", lambda repo_root=None: False)
    from rebar.llm.code_review import review_code

    r = review_code(diff_text=_DIFF, changed_files=["x.py"])
    schemas.validator(schemas.REVIEW_RESULT).validate(r)
    assert r["findings"] == [] and "disabled" in r["summary"].lower()


# ── enabled → runs the gate, schema-valid verdict ───────────────────────────────────────────
def test_produce_verdict_enabled_runs_gate_and_validates(monkeypatch):
    monkeypatch.setattr(gate_dispatch, "code_review_enabled", lambda repo_root=None: True)
    # focus on the LLM-verdict path: the WS5 security-detector fail-closed is its own suite, and
    # without a repo it would otherwise scan the cwd. Stub it to a no-op here.
    from rebar.llm.code_review import detectors as _det

    monkeypatch.setattr(_det, "run_security_detectors", lambda **kw: {})
    v = gate_dispatch.produce_code_review_verdict(
        gate_dispatch.CodeReviewRequest(
            LLMConfig.from_env(),
            diff_text=_DIFF,
            changed_files=["x.py"],
            runner=FakeRunner(structured=_STRUCTURED),
        )
    )
    assert v["verdict"] in ("PASS", "BLOCK")
    assert "coverage" in v and v["coverage"].get("llm_ran") is True
    schemas.validator(schemas.CODE_REVIEW_VERDICT).validate(v)


def test_produce_verdict_enabled_override_forces_run_when_config_disabled(monkeypatch):
    # WS6 force-enable (ADR 0015): the reviewed repo has code_review_enabled=False, but the Gerrit
    # voter passes enabled=True — the gate MUST run (not return the inert disabled verdict).
    monkeypatch.setattr(gate_dispatch, "code_review_enabled", lambda repo_root=None: False)
    from rebar.llm.code_review import detectors as _det

    monkeypatch.setattr(_det, "run_security_detectors", lambda **kw: {})  # focus on the gate path
    v = gate_dispatch.produce_code_review_verdict(
        gate_dispatch.CodeReviewRequest(
            LLMConfig.from_env(),
            diff_text=_DIFF,
            changed_files=["x.py"],
            runner=FakeRunner(structured=_STRUCTURED),
            enabled=True,
        )
    )
    assert v["coverage"].get("enabled") is not False  # NOT the inert disabled verdict
    assert v["coverage"].get("llm_ran") is True  # the gate actually ran


def test_produce_verdict_enabled_false_forces_inert_when_config_enabled(monkeypatch):
    # The override is symmetric: enabled=False forces the inert verdict + zero LLM calls even when
    # the config says the gate is enabled.
    monkeypatch.setattr(gate_dispatch, "code_review_enabled", lambda repo_root=None: True)
    runner = _CountingRunner(structured=_STRUCTURED)
    v = gate_dispatch.produce_code_review_verdict(
        gate_dispatch.CodeReviewRequest(
            LLMConfig.from_env(), diff_text=_DIFF, runner=runner, enabled=False
        )
    )
    assert v["verdict"] == "PASS" and v["coverage"]["enabled"] is False
    assert runner.calls == 0  # inert: the override short-circuited before any LLM call


def test_review_code_enabled_translates_verdict_to_review_result(monkeypatch):
    monkeypatch.setattr(gate_dispatch, "code_review_enabled", lambda repo_root=None: True)
    from rebar.llm.code_review import detectors as _det
    from rebar.llm.code_review import review_code

    monkeypatch.setattr(_det, "run_security_detectors", lambda **kw: {})  # focus on LLM path
    r = review_code(
        diff_text=_DIFF, changed_files=["x.py"], runner=FakeRunner(structured=_STRUCTURED)
    )
    schemas.validator(schemas.REVIEW_RESULT).validate(r)
    assert r["target"]["kind"] == "code"
    assert "verdict" in r  # the typed gate verdict is attached for callers that want it


# ── outage → INDETERMINATE, never a hollow PASS ─────────────────────────────────────────────
def test_produce_verdict_degrades_to_indeterminate_on_outage(monkeypatch):
    monkeypatch.setattr(gate_dispatch, "code_review_enabled", lambda repo_root=None: True)
    v = gate_dispatch.produce_code_review_verdict(
        gate_dispatch.CodeReviewRequest(
            LLMConfig.from_env(), diff_text=_DIFF, runner=_OutageRunner(structured=_STRUCTURED)
        )
    )
    assert v["verdict"] == "INDETERMINATE"
    assert v["coverage"]["llm_unavailable"] is True


# ── shim translation: a gate verdict's findings → common.finding ────────────────────────────
def test_shim_translates_kernel_findings_to_common_findings():
    from rebar.llm.code_review.shim import _verdict_to_review_result

    verdict = {
        "verdict": "BLOCK",
        "blocking": [
            {
                "finding": "SQL injection in the query builder",
                "criteria": ["security"],
                "evidence": ["q.py:42"],
                "location": "q.py:42",
                "severity": "critical",
                "reviewer_id": "code-review-security",
            }
        ],
        "advisory": [],
        "runner": "fake",
        "model": None,
    }
    r = _verdict_to_review_result(verdict, base="HEAD~1", head="HEAD", changed_files=["q.py"])
    schemas.validator(schemas.REVIEW_RESULT).validate(r)
    f = r["findings"][0]
    assert f["severity"] == "critical"  # the POST-Pass-3 severity
    assert f["dimension"] == "security"  # criteria[0]
    assert "SQL injection" in f["detail"]
    assert f["citations"][0] == {"kind": "file", "path": "q.py", "line_start": 42}
    assert r["reviewers"] == ["code-review-security"]
    assert r["verdict"]["verdict"] == "BLOCK"  # raw verdict attached


@pytest.mark.parametrize(
    ("kernel_sev", "common_sev"),
    [("critical", "critical"), ("major", "high"), ("minor", "medium"), ("none", "info")],
)
def test_shim_maps_kernel_severity_to_common_vocabulary(kernel_sev, common_sev):
    """The kernel Pass-3 severity vocab ({critical,major,minor,none}) must MAP to the
    common.finding enum ({critical,high,medium,low,info}) — not pass through (which would clamp
    major/minor/none to 'info', flattening every non-critical finding)."""
    from rebar.llm.code_review.shim import _verdict_to_review_result

    verdict = {
        "verdict": "PASS",
        "blocking": [],
        "advisory": [{"finding": "x", "criteria": ["correctness"], "severity": kernel_sev}],
        "runner": "fake",
        "model": None,
    }
    r = _verdict_to_review_result(verdict, base="HEAD~1", head="HEAD", changed_files=["a.py"])
    schemas.validator(schemas.REVIEW_RESULT).validate(r)
    assert r["findings"][0]["severity"] == common_sev


# ── sidecar: emits on an explicit target ticket only ────────────────────────────────────────
def test_sidecar_payload_anchors_on_the_explicit_target_ticket():
    from rebar.llm.code_review import sidecar

    payload = sidecar.build_payload(
        {"verdict": "PASS", "blocking": [], "advisory": []}, target_ticket="abc-123"
    )
    assert payload["ticket_id"] == "abc-123"
    # story 7c84: a fresh emit is now the lossless v2 record
    assert payload["schema"] == "code_review_result_v2"
    assert payload["verdict"] == "PASS"
    # emit with a falsy ticket is a no-op (the diff-only path emits nothing).
    assert sidecar.emit({"verdict": "PASS"}, target_ticket="") is False


# ── flag + source-separation pins ───────────────────────────────────────────────────────────
def test_flag_defaults_off_and_is_env_overridable(monkeypatch, tmp_path):
    import rebar.config as _config

    monkeypatch.delenv("REBAR_VERIFY_ENABLE_CODE_REVIEW", raising=False)
    assert _config.load_config(str(tmp_path)).verify.enable_code_review is False
    monkeypatch.setenv("REBAR_VERIFY_ENABLE_CODE_REVIEW", "1")
    assert _config.load_config(str(tmp_path)).verify.enable_code_review is True


def test_single_pass_route_is_retired():
    # the single-pass module is gone; review_code is the gate-backed shim.
    import rebar.llm.code_review as cr

    assert cr.review_code.__module__ == "rebar.llm.code_review.shim"
    with pytest.raises(ModuleNotFoundError):
        import rebar.llm.code_review.single_pass  # noqa: F401
    assert not hasattr(cr, "select_code_reviewers")  # retired single-pass helper
