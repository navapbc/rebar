"""Optional OTel tracing for the agent runtime (story d6d1).

WIRED, write-only, best-effort: when the ``[tracing]`` extra is installed AND Langfuse keys
are configured, the pydantic-ai runtime's agent/LLM/tool spans are exported via OTLP to
Langfuse (which is an OTLP *endpoint*, not an SDK dependency here). It is a NO-OP without the
extra or the keys, and ANY setup failure degrades silently to "no tracing" — tracing must
never break or alter an operation (oracle discipline: a sink is never read back into a rebar
decision). Imports of opentelemetry/pydantic-ai are INSIDE the function so importing this
module stays dependency-free.

This is wired but not live-verified (per the d6d1 decision); enabling it requires the
``[tracing]`` extra + LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY (+ optional LANGFUSE_HOST).
"""

from __future__ import annotations

import base64

from rebar.llm.config import LangfuseConfig

_CONFIGURED = False


def setup_tracing(langfuse: LangfuseConfig | None = None) -> bool:
    """Enable OTLP→Langfuse tracing for the pydantic-ai runtime, best-effort and idempotent.

    Returns True when tracing is (or was already) active, False when it is a no-op (no
    ``[tracing]`` extra, no Langfuse keys, or a setup error). Never raises."""
    global _CONFIGURED
    if _CONFIGURED:
        return True
    cfg = langfuse or LangfuseConfig.from_env()
    if not cfg.enabled:  # no keys → nothing to export to
        return False
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from pydantic_ai import Agent
    except Exception:  # noqa: BLE001 - [tracing] extra (or pydantic-ai) absent → no-op
        return False
    try:
        host = (cfg.host or "https://cloud.langfuse.com").rstrip("/")
        endpoint = f"{host}/api/public/otel/v1/traces"
        auth = base64.b64encode(f"{cfg.public_key}:{cfg.secret_key}".encode()).decode()
        provider = TracerProvider()
        provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(endpoint=endpoint, headers={"Authorization": f"Basic {auth}"})
            )
        )
        trace.set_tracer_provider(provider)
        Agent.instrument_all()  # emit agent/LLM/tool spans through the configured provider
        _CONFIGURED = True
        return True
    except Exception:  # noqa: BLE001 - tracing must never break an operation
        return False
