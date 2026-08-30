"""Optional OTel tracing (story d6d1): no-op without keys, import-light, never raises."""

from __future__ import annotations

import subprocess
import sys
import types


def test_setup_tracing_is_noop_without_keys() -> None:
    # No Langfuse keys configured → tracing must be a clean no-op (False), never raising,
    # and must not require the [tracing] extra to be reached.
    from rebar.llm.config import LangfuseConfig
    from rebar.llm.tracing import setup_tracing

    cfg = LangfuseConfig(public_key=None, secret_key=None, host=None)
    assert cfg.enabled is False
    assert setup_tracing(cfg) is False


def test_setup_tracing_swallows_a_configured_setup_failure(monkeypatch) -> None:
    # ticket 6586 (Refactor runner orchestration along the proven lifecycle seam): the
    # runner-lifecycle Testing section names "telemetry failure" as one of the fault-matrix
    # boundaries `run()` must survive. `setup_tracing`'s own docstring already promises
    # "never raises" for ANY setup error, not merely the no-keys/no-extra case the sibling
    # test above pins — but nothing previously drove the second `try/except` (the one
    # guarding the ACTUAL provider/exporter construction, reached only once both keys are
    # present and the `[tracing]` extra imports succeed). Inject fake `opentelemetry`
    # submodules so those imports succeed, then make `TracerProvider()` raise — proving the
    # swallow is real, not just untriggered dead code, so `run()`'s `setup_tracing(cfg.langfuse)`
    # call site (best-effort, write-only, never read back into a decision) stays safe even
    # when Langfuse keys ARE configured and the OTLP setup itself is what fails.
    from rebar.llm import tracing
    from rebar.llm.config import LangfuseConfig

    monkeypatch.setattr(tracing, "_CONFIGURED", False)

    trace_mod = types.ModuleType("opentelemetry.trace")
    trace_mod.set_tracer_provider = lambda provider: None
    otel_mod = types.ModuleType("opentelemetry")
    otel_mod.trace = trace_mod
    exporter_mod = types.ModuleType("opentelemetry.exporter.otlp.proto.http.trace_exporter")

    class _FakeExporter:
        def __init__(self, *args, **kwargs):
            pass

    exporter_mod.OTLPSpanExporter = _FakeExporter
    sdk_trace_mod = types.ModuleType("opentelemetry.sdk.trace")

    class _RaisingTracerProvider:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("boom: tracer provider init failed")

    sdk_trace_mod.TracerProvider = _RaisingTracerProvider
    sdk_export_mod = types.ModuleType("opentelemetry.sdk.trace.export")
    sdk_export_mod.BatchSpanProcessor = lambda *args, **kwargs: None

    fakes = {
        "opentelemetry": otel_mod,
        "opentelemetry.trace": trace_mod,
        "opentelemetry.exporter": types.ModuleType("opentelemetry.exporter"),
        "opentelemetry.exporter.otlp": types.ModuleType("opentelemetry.exporter.otlp"),
        "opentelemetry.exporter.otlp.proto": types.ModuleType("opentelemetry.exporter.otlp.proto"),
        "opentelemetry.exporter.otlp.proto.http": types.ModuleType(
            "opentelemetry.exporter.otlp.proto.http"
        ),
        "opentelemetry.exporter.otlp.proto.http.trace_exporter": exporter_mod,
        "opentelemetry.sdk": types.ModuleType("opentelemetry.sdk"),
        "opentelemetry.sdk.trace": sdk_trace_mod,
        "opentelemetry.sdk.trace.export": sdk_export_mod,
    }
    for name, module in fakes.items():
        monkeypatch.setitem(sys.modules, name, module)

    cfg = LangfuseConfig(public_key="pk", secret_key="sk", host=None)
    assert cfg.enabled is True
    assert tracing.setup_tracing(cfg) is False
    assert tracing._CONFIGURED is False


def test_importing_tracing_pulls_no_opentelemetry() -> None:
    # `import rebar.llm.tracing` must stay dependency-free (opentelemetry/pydantic_ai are
    # imported INSIDE setup_tracing) — checked in a clean subprocess.
    code = (
        "import sys, rebar.llm.tracing;"
        "heavy=[m for m in ('opentelemetry','pydantic_ai') if m in sys.modules];"
        "print('CLEAN' if not heavy else 'HEAVY', heavy)"
    )
    cp = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env={"PYTHONPATH": "src", "PATH": __import__("os").environ.get("PATH", "")},
        check=False,
    )
    assert cp.returncode == 0, cp.stderr
    assert cp.stdout.startswith("CLEAN"), cp.stdout
