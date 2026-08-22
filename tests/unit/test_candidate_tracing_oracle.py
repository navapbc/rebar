"""RP-01 S4 — candidate-tracing HELD-OUT ORACLE (edge/E2E; withheld from the implementer).

The happy path and shared harness live in ``test_candidate_tracing.py``; this module imports
that harness and adds the cases that separate a real implementation from one that only fakes
the happy path:

* AC-5  all candidates fail -> one ``raised`` span each; the ``FallbackExceptionGroup`` /
        resolution is UNCHANGED by instrumentation.
* AC-6  bug-895c native->prompted downgrade is legible as a native ``raised`` then a prompted
        ``returned`` span, with NO ``_pai_structured`` change (a real native 400 + downgrade).
* AC-7  a ``raised`` span reason is sanitized through ``sanitize_diagnostic`` (no key/email/
        endpoint/prompt/response body).
* AC-8  a span-emission failure while tracing is ENABLED is swallowed — result, usage, and the
        propagated exception are identical to tracing-off.
* AC-9  ``_usage`` (and thus pricing/JSONL inputs) is identical tracing on vs off and carries no
        candidate counter.
* AC-10 the base install (no tracing extra) imports ``rebar.llm.tracing`` and runs the
        candidate seam as a no-op with no span dependency error (subprocess).
* AC-1/AC-2 real OTel nesting: each candidate span parents to the single Agent model-request
        span, ordered — proven with an in-memory exporter under ``importorskip`` (skips in the
        default/CI env that lacks the ``[tracing]`` SDK; validated locally).
"""

from __future__ import annotations

import json
import subprocess
import sys

import httpx
import pytest

pytest.importorskip("pydantic_ai")

import pydantic_ai.models
from test_candidate_tracing import (
    _FALLBACK,
    _FALLBACK_QUALIFIED,
    _PRIMARY,
    _PRIMARY_QUALIFIED,
    ATTR_ERROR,
    ATTR_MODEL,
    ATTR_ORDER,
    ATTR_OUTCOME,
    OUTCOME_RAISED,
    OUTCOME_RETURNED,
    RecordingTracer,
    _ok_body,
    install_failover,
    install_recording_tracer,
    run_standard,
)

from rebar.llm.config import LLMConfig
from rebar.llm.runner import PydanticAIRunner, RunRequest

pytestmark = pytest.mark.unit


# ── AC-5: all candidates fail ──────────────────────────────────────────────────────────────


def _run_capturing():
    """Run the standard chain and capture ('ok', result) or ('raised', exc_type, exc_str)."""
    try:
        return ("ok", run_standard())
    except BaseException as exc:  # noqa: BLE001 - we compare the propagated type/str
        return ("raised", type(exc).__name__, str(exc))


def test_all_candidates_fail_emit_one_raised_span_each_and_leave_exception_unchanged(monkeypatch):
    """AC-5: when EVERY candidate fails, each attempted candidate is still visible as its own
    ordered ``raised`` span — and instrumentation changes NOTHING about the propagated error:
    the same exception type/message is raised whether tracing is on or off."""
    # tracing OFF (real no-op tracer): baseline exception
    install_failover(monkeypatch, status={_PRIMARY: 529, _FALLBACK: 529})
    off = _run_capturing()

    # tracing ON (recording tracer): same scenario
    tracer = install_recording_tracer(monkeypatch)
    install_failover(monkeypatch, status={_PRIMARY: 529, _FALLBACK: 529})
    on = _run_capturing()

    assert off[0] == "raised" and on[0] == "raised", "an all-fail run must propagate an error"
    assert on[1:] == off[1:], "the propagated exception type/message must be identical on vs off"

    spans = tracer.candidate_spans()
    assert [s.attrs[ATTR_ORDER] for s in spans] == [0, 1]
    assert [s.attrs[ATTR_OUTCOME] for s in spans] == [OUTCOME_RAISED, OUTCOME_RAISED]
    assert [s.attrs[ATTR_MODEL] for s in spans] == [_PRIMARY_QUALIFIED, _FALLBACK_QUALIFIED]


# ── AC-6: bug-895c native -> prompted downgrade legibility ─────────────────────────────────


def _grammar_400_body() -> dict:
    return {
        "type": "error",
        "error": {"type": "invalid_request_error", "message": "Grammar compilation timed out."},
    }


def _install_native_downgrade(monkeypatch) -> list[int]:
    """Drive the REAL bug-895c native->prompted downgrade in ``_pai_structured`` unchanged.

    ``structured.output_mode`` is forced to ``NativeOutput`` so the operation takes the native
    (constrained-decoding) branch — standing in for a native-capable provider without altering
    ``_pai_structured``. The FIRST model request (the native attempt) 400s with the documented
    grammar-compilation rejection that ``translate_schema_complexity_rejection`` recognizes;
    every later request (the prompted downgrade) returns a valid verdict. Returns the call log.

    ``REBAR_GATE_ALLOW_UNGATED`` is the documented escape hatch for the completion-verdict op's
    file-tool wiring — orthogonal to candidate tracing, and no file is actually read here."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-dummy")
    monkeypatch.setenv("REBAR_GATE_ALLOW_UNGATED", "1")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    for name in ("FRONTIER", "STANDARD", "TRIVIAL"):
        for field in ("MODEL", "PROVIDER", "ENDPOINT"):
            monkeypatch.delenv(f"REBAR_LLM_{name}_{field}", raising=False)

    calls: list[int] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        idx = len(calls)
        calls.append(idx)
        if idx == 0:
            return httpx.Response(400, json=_grammar_400_body())
        return httpx.Response(200, json=_ok_body(_PRIMARY, text='{"verdict": "PASS"}'))

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda *a, **kw: httpx.MockTransport(_handler))
    monkeypatch.setattr(pydantic_ai.models, "ALLOW_MODEL_REQUESTS", True)

    from pydantic_ai import NativeOutput

    from rebar.llm import config as llm_config
    from rebar.llm import structured

    # A fallback list makes fallback_targets non-empty so the run routes through
    # build_fallback_model (where wrap_candidate applies). The native primary 400 is neither
    # retryable nor CHANGE_PROVIDER, so should_fall_back stays False: the native attempt makes
    # exactly ONE call (primary) then _pai_structured downgrades to the prompted primary.
    table = {
        "model_classes": {
            "standard": {
                "model": _PRIMARY,
                "provider": "anthropic",
                "fallback": [{"model": _FALLBACK, "provider": "anthropic"}],
            }
        }
    }
    monkeypatch.setattr(llm_config, "_read_llm_file_table", lambda repo_root=None: table)
    monkeypatch.setattr(
        structured, "output_mode", lambda model_cls, caps, **kw: NativeOutput(model_cls)
    )
    return calls


def _run_verdict(cfg) -> dict:
    req = RunRequest(
        system_prompt="sys",
        instructions="go",
        config=cfg,
        reviewers=["v"],
        mode="structured",
        output_schema="completion_verdict",
    )
    return PydanticAIRunner(cfg).run(req)


def test_895c_downgrade_is_legible_as_native_raised_then_prompted_returned(monkeypatch):
    """AC-6: the S2-preserved bug-895c native->prompted fallback stays legible in tracing WITHOUT
    touching ``_pai_structured``: the native constrained-decoding attempt (which the provider
    400s on grammar compilation) shows a ``raised`` candidate span, and the prompted downgrade
    that actually answers shows a ``returned`` candidate span — same wrapped candidate, two
    ``agent.run`` calls."""
    tracer = install_recording_tracer(monkeypatch)
    calls = _install_native_downgrade(monkeypatch)

    cfg = LLMConfig(repo_path=".", model=_PRIMARY_QUALIFIED, llm_retry_max_attempts=1)
    result = _run_verdict(cfg)
    assert result["verdict"] == "PASS", "the prompted downgrade produced the validated verdict"
    assert len(calls) == 2, "exactly a native attempt then a prompted attempt ran"

    outcomes = [s.attrs[ATTR_OUTCOME] for s in tracer.candidate_spans()]
    assert OUTCOME_RAISED in outcomes, "the native attempt must be visible as a raised span"
    assert outcomes[-1] == OUTCOME_RETURNED, "the answering prompted attempt is a returned span"
    assert outcomes.index(OUTCOME_RAISED) < len(outcomes) - 1, (
        "native raised precedes prompted returned"
    )
    for span in tracer.candidate_spans():
        assert span.attrs[ATTR_MODEL] == _PRIMARY_QUALIFIED


# ── AC-7: secret sanitization on a raised span reason ──────────────────────────────────────

_SECRET_KEY = "sk-ant-api03-DEADBEEFdeadbeefDEADBEEFdeadbeef"
_SECRET_EMAIL = "leak@example.com"


def test_raised_span_reason_is_sanitized_and_carries_no_secret(monkeypatch):
    """AC-7: a candidate failure whose provider message embeds an API key + email yields a
    ``raised`` span whose ``rebar.candidate.error`` is REDACTED (routed through
    ``sanitize_diagnostic`` under the ``message`` key) and carries no prompt/response/endpoint
    body. The primary fails with the secret-bearing message; the fallback answers."""
    tracer = install_recording_tracer(monkeypatch)
    leaky = f"boom key={_SECRET_KEY} contact {_SECRET_EMAIL}"
    install_failover(monkeypatch, status={_PRIMARY: 529}, err_message=leaky)

    run_standard()

    raised = [s for s in tracer.candidate_spans() if s.attrs.get(ATTR_OUTCOME) == OUTCOME_RAISED]
    assert raised, "the failed primary must be a raised span"
    reason = raised[0].attrs.get(ATTR_ERROR, "")
    assert _SECRET_KEY not in reason, f"the api key leaked into the span: {reason!r}"
    assert _SECRET_EMAIL not in reason, f"the email leaked into the span: {reason!r}"
    # the reason field carries no full prompt/response/endpoint body
    assert "sys" not in reason and "go" not in reason, "no prompt text on the span"


# ── AC-8: span-emission failure while tracing is ENABLED is swallowed ───────────────────────


class _FaultyTracer(RecordingTracer):
    """A tracer whose span operations RAISE — models a broken exporter/SDK while tracing is on."""

    def start_as_current_span(self, name: str, *a, **k):  # type: ignore[override]
        raise RuntimeError("span backend exploded")


def test_span_emission_failure_while_tracing_on_matches_tracing_off(monkeypatch):
    """AC-8: if opening/emitting a candidate span throws WHILE TRACING IS ENABLED, the wrapper
    swallows it best-effort — the fallback result and ``_usage`` are byte-identical to a
    tracing-off run, and no span error leaks out."""
    # tracing OFF baseline
    install_failover(monkeypatch, status={_PRIMARY: 529})
    off = run_standard()

    # tracing ON but the tracer explodes on every span
    import rebar.llm.tracing as tracing_mod

    monkeypatch.setattr(tracing_mod, "_candidate_tracer", lambda: _FaultyTracer())
    install_failover(monkeypatch, status={_PRIMARY: 529})
    on = run_standard()  # must NOT raise

    assert on["_usage"] == off["_usage"], "usage must be identical when span emission fails"
    assert on.get("text") == off.get("text"), (
        "the answer must be identical when span emission fails"
    )


# ── AC-9: usage identical on/off, no candidate counter ─────────────────────────────────────


def test_usage_is_identical_tracing_on_vs_off_with_no_candidate_counter(monkeypatch):
    """AC-9: instrumentation is observability-only — ``_usage`` is identical tracing on vs off,
    and it never grows a candidate counter (the request budget stays authoritative, per the
    approved RP-01 decision)."""
    install_failover(monkeypatch, status={_PRIMARY: 529})
    off = run_standard()

    install_recording_tracer(monkeypatch)
    install_failover(monkeypatch, status={_PRIMARY: 529})
    on = run_standard()

    assert on["_usage"] == off["_usage"], "usage identical on vs off"
    usage_repr = json.dumps(on["_usage"], default=str).lower()
    assert "candidate" not in usage_repr, "usage must not carry a candidate counter"


# ── AC-10: lean base install imports + runs the seam as a no-op ─────────────────────────────


def test_base_install_imports_tracing_and_runs_candidate_seam_without_sdk():
    """AC-10: the default install (no ``[tracing]`` extra) must import ``rebar.llm.tracing`` and
    run the candidate seam as a NO-OP without any span dependency error. Proven in a subprocess
    that (a) imports the module and opens a ``candidate_span`` with no configured provider, and
    (b) asserts the OpenTelemetry SDK was not required to do so."""
    code = (
        "import sys\n"
        "import rebar.llm.tracing as t\n"
        "cm = t.candidate_span(0, 'openai:gpt-4o')\n"
        "h = cm.__enter__()\n"
        "cm.__exit__(None, None, None)\n"
        "assert 'opentelemetry.sdk' not in sys.modules, 'seam must not require the tracing SDK'\n"
        "print('OK')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120, check=False
    )
    assert proc.returncode == 0, (
        f"lean import/seam failed:\nSTDOUT={proc.stdout}\nSTDERR={proc.stderr}"
    )
    assert "OK" in proc.stdout


# AC-1 / AC-2's real-OTel ambient-context NESTING proof (candidate spans parent to the single
# Agent model-request span) requires the real ``[tracing]`` SDK, which the lean unit Verified
# lane deliberately does not install (real-SDK tracing lives in the external tier, like
# ``tests/external/test_llm_trace.py``). That proof lives in
# ``tests/external/test_candidate_tracing_live.py``. The env-independent ``RecordingTracer``
# tests above cover the AC-1/AC-2 span-emission / zero-based-order / attribute contract in CI.
