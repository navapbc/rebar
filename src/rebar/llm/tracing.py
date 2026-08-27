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
import contextlib
from typing import Any

from rebar.llm.config import LangfuseConfig

_CONFIGURED = False


class _NoOpSpan:
    """A span that discards every attribute — the fallback when no OTel API is present."""

    def set_attribute(self, key: str, value: object) -> None:
        pass


class _NoOpTracer:
    """A tracer whose spans record nothing — used when the OTel API import fails."""

    @contextlib.contextmanager
    def start_as_current_span(self, _name: str, *_a: Any, **_k: Any):
        yield _NoOpSpan()


def _candidate_tracer() -> Any:
    """Return the tracer that owns per-candidate spans (the monkeypatch seam).

    Uses the OTel *API* (``get_tracer``) lazily so importing this module never requires the
    ``[tracing]`` SDK; if the API is absent, returns a no-op tracer so callers still get a
    usable ``start_as_current_span`` context manager."""
    try:
        from opentelemetry import trace
    except Exception:  # noqa: BLE001 - OTel API absent → no-op tracer
        return _NoOpTracer()
    return trace.get_tracer("rebar.llm.candidate")


def _set_attr(span: Any, key: str, value: object) -> None:
    """Record one attribute onto ``span``, swallowing any failure.

    Tracing must never break an operation, so a span object that rejects the attribute (or is
    not a span at all) is simply ignored."""
    try:
        span.set_attribute(key, value)
    except Exception:  # noqa: BLE001 - tracing must never break an operation
        pass


class _CandidateHandle:
    """A tiny handle that records the outcome of one candidate onto its span, best-effort."""

    def __init__(self, span: Any) -> None:
        self._span = span

    def _set(self, key: str, value: object) -> None:
        _set_attr(self._span, key, value)

    def returned(self, response: Any) -> None:
        self._set("rebar.candidate.outcome", "returned")
        usage = getattr(response, "usage", None)
        inp = getattr(usage, "input_tokens", None)
        out = getattr(usage, "output_tokens", None)
        if isinstance(inp, int):
            self._set("rebar.candidate.usage.input_tokens", inp)
        if isinstance(out, int):
            self._set("rebar.candidate.usage.output_tokens", out)

    def raised(self, exc: BaseException) -> None:
        self._set("rebar.candidate.outcome", "raised")
        try:
            from rebar.llm.failure import sanitize_diagnostic

            reason = sanitize_diagnostic({"message": str(exc)}).get("message")
        except Exception:  # noqa: BLE001 - tracing must never break an operation
            reason = None
        if reason is not None:
            self._set("rebar.candidate.error", reason)


@contextlib.contextmanager
def candidate_span(order: int, candidate: str):
    """Open ONE rebar-owned child span for candidate ``order`` (``candidate`` = its
    provider-qualified model id), yielding a handle whose ``returned``/``raised`` record the
    outcome ONTO that span.

    Purely additive observability: every span operation is best-effort and swallows any
    exception, and if the tracer cannot even open a span the block still yields a usable no-op
    handle — so a tracing fault never escapes into (or alters) the request path. The caller's
    own exception, however, propagates unchanged."""
    cm = None
    span: Any = _NoOpSpan()
    try:
        cm = _candidate_tracer().start_as_current_span(f"candidate {order}")
        span = cm.__enter__()
    except Exception:  # noqa: BLE001 - tracing must never break an operation
        cm = None
        span = _NoOpSpan()
    handle = _CandidateHandle(span)
    handle._set("rebar.candidate.order", order)
    handle._set("gen_ai.request.model", candidate)
    try:
        yield handle
    except BaseException:
        _close_span(cm, capture=True)
        raise
    else:
        _close_span(cm, capture=False)


def _close_span(cm: Any, *, capture: bool) -> None:
    """Best-effort close of a started span context manager; never raises."""
    if cm is None:
        return
    try:
        if capture:
            import sys

            cm.__exit__(*sys.exc_info())
        else:
            cm.__exit__(None, None, None)
    except Exception:  # noqa: BLE001 - tracing must never break an operation
        pass


def _run_tracer() -> Any:
    """Return the tracer that owns per-gate-run root spans (the monkeypatch seam).

    Same lazy-API discipline as :func:`_candidate_tracer`: the OTel *API* import lives inside the
    function so importing this module never requires the ``[tracing]`` extra, and an absent API
    degrades to a no-op tracer."""
    try:
        from opentelemetry import trace
    except Exception:  # noqa: BLE001 - OTel API absent -> no-op tracer
        return _NoOpTracer()
    return trace.get_tracer("rebar.llm.run")


@contextlib.contextmanager
def run_span(operation: str, *, ticket_id: str | None = None):
    """Open ONE rebar-owned root span for a whole gate run, so every candidate span opened
    inside it nests beneath it and the run is a SINGLE trace.

    ``operation`` is a canonical rebar verb (``"review-plan"`` / ``"verify-completion"`` /
    ``"review-code"``); ``ticket_id`` is recorded when the boundary has one (``review-code``
    does not). Both are best-effort attributes.

    Fail-open exactly like :func:`candidate_span`, and more load-bearingly so: this span sits at
    the RUN boundary, so an unguarded tracing fault would abort the entire gate run. An absent
    tracer, a ``get_tracer`` that raises, a ``start_as_current_span`` that raises, and a
    ``set_attribute`` that raises are all swallowed — the ``with`` body still runs. The caller's
    own exception propagates unchanged."""
    cm = None
    span: Any = _NoOpSpan()
    try:
        cm = _run_tracer().start_as_current_span(f"rebar {operation}")
        span = cm.__enter__()
    except Exception:  # noqa: BLE001 - tracing must never break an operation
        cm = None
        span = _NoOpSpan()
    _set_attr(span, "rebar.run.operation", operation)
    if ticket_id is not None:
        _set_attr(span, "rebar.run.ticket_id", ticket_id)
    try:
        yield span
    except BaseException:
        _close_span(cm, capture=True)
        raise
    else:
        _close_span(cm, capture=False)


def wrap_candidate(model: Any, order: int, candidate: str) -> Any:
    """Wrap ``model`` in a transparent ``WrapperModel`` subclass that opens a
    :func:`candidate_span` around each ``request``.

    ``WrapperModel`` delegates model_name/profile/provider/settings/``__getattr__``, so the
    wrapped model is behaviorally identical — only an extra child span is emitted. Returns the
    original model unchanged if the wrapper class cannot be built (tracing stays best-effort)."""
    try:
        cls = _candidate_wrapper_class()
    except Exception:  # noqa: BLE001 - pydantic-ai absent → no wrapping, no tracing
        return model
    try:
        return cls(model, order, candidate)
    except Exception:  # noqa: BLE001 - tracing must never break an operation
        return model


_CANDIDATE_WRAPPER: Any = None


def _candidate_wrapper_class() -> Any:
    """Build (once) the ``WrapperModel`` subclass that traces each ``request``."""
    global _CANDIDATE_WRAPPER
    if _CANDIDATE_WRAPPER is not None:
        return _CANDIDATE_WRAPPER
    from pydantic_ai.models.wrapper import WrapperModel

    class _TracedCandidateModel(WrapperModel):
        def __init__(self, wrapped: Any, order: int, candidate: str) -> None:
            super().__init__(wrapped)
            object.__setattr__(self, "_candidate_order", order)
            object.__setattr__(self, "_candidate_id", candidate)

        # ``WrapperModel`` overrides most delegating properties but leaves these three base
        # ``Model`` defaults in place, so forward them explicitly to stay fully transparent.
        @property
        def base_url(self) -> Any:
            return self.wrapped.base_url

        @property
        def label(self) -> Any:
            return self.wrapped.label

        @property
        def model_id(self) -> Any:
            return self.wrapped.model_id

        async def request(
            self, messages: Any, model_settings: Any, model_request_parameters: Any
        ) -> Any:
            with candidate_span(self._candidate_order, self._candidate_id) as h:
                try:
                    resp = await super().request(messages, model_settings, model_request_parameters)
                except Exception as exc:
                    h.raised(exc)
                    raise
                h.returned(resp)
                return resp

    _CANDIDATE_WRAPPER = _TracedCandidateModel
    return _CANDIDATE_WRAPPER


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
