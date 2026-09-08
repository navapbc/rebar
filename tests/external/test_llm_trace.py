"""External Langfuse OTLP round trip for a review run.

``setup_tracing`` exports OpenTelemetry spans through a batch processor and instruments the
agent. This test runs a configured-provider review, flushes the processor, and queries the
public REST API for a newly ingested trace with observations. A bounded retry handles
asynchronous ingestion without adding the Langfuse SDK as a dependency.

The test requires ``REBAR_RUN_EXTERNAL``, the agents and tracing extras, a provider credential,
and a configured Langfuse endpoint.
"""

from __future__ import annotations

import base64
import datetime as _dt
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

import rebar

pytestmark = pytest.mark.external

_READ_TIMEOUT_S = 90.0  # ingestion is async; poll up to this long after force_flush()
_READ_INTERVAL_S = 1.0
# Guard against host/Langfuse clock skew when filtering traces by fromTimestamp.
_SKEW_MARGIN_S = 300.0


def _live_model() -> str | None:
    try:
        import rebar.llm as llm
    except ImportError:
        return None
    if not llm.agents_extra_installed():
        return None
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "claude-opus-4-8"
    if os.environ.get("OPENAI_API_KEY"):
        return "gpt-4o"
    return None


def _tracing_extra_installed() -> bool:
    """True when the ``[tracing]`` extra (OTel SDK + OTLP exporter) is importable —
    without it ``setup_tracing`` is a silent no-op and nothing is exported."""
    try:
        import opentelemetry.exporter.otlp.proto.http.trace_exporter
        import opentelemetry.sdk.trace  # noqa: F401
    except Exception:  # noqa: BLE001 — extra absent
        return False
    return True


_MODEL = _live_model()
_LF_HOST = os.environ.get("LANGFUSE_HOST")
_LF_PK = os.environ.get("LANGFUSE_PUBLIC_KEY")
_LF_SK = os.environ.get("LANGFUSE_SECRET_KEY")
_lf_ready = bool(_MODEL and _LF_HOST and _LF_PK and _LF_SK and _tracing_extra_installed())
_skip = pytest.mark.skipif(
    not _lf_ready,
    reason=(
        "needs an LLM key + agents extra + tracing extra + LANGFUSE_HOST/PUBLIC_KEY/SECRET_KEY"
    ),
)


def _lf_get(path: str) -> dict | None:
    """GET a Langfuse public-API path with HTTP Basic auth (public:secret). Returns the
    decoded JSON, or None on 404 (not yet ingested) — raising on other HTTP errors."""
    url = f"{_LF_HOST.rstrip('/')}{path}"
    token = base64.b64encode(f"{_LF_PK}:{_LF_SK}".encode()).decode()
    req = urllib.request.Request(url, headers={"Authorization": f"Basic {token}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None  # async ingestion lag — caller retries
        raise


def _force_flush_tracing() -> None:
    """Flush the global tracer provider's BatchSpanProcessor so the review's spans are
    exported to the OTLP endpoint NOW, instead of on the batch processor's own timer."""
    try:
        from opentelemetry import trace

        provider = trace.get_tracer_provider()
        flush = getattr(provider, "force_flush", None)
        if callable(flush):
            flush()
    except Exception:  # noqa: BLE001 — never let a flush failure mask the assertion
        pass


@_skip
def test_live_review_exports_langfuse_trace(rebar_repo: Path) -> None:
    import rebar.llm as llm
    from rebar.llm.config import LLMConfig

    epic = rebar.create_ticket(
        "epic",
        "Add login",
        description="Build login.\n\n## Acceptance Criteria\n- [ ] users can log in",
        repo_root=str(rebar_repo),
    )
    # Seed a concrete file with an obvious issue so the agent CONVERGES quickly
    # (finds it, reports, stops) instead of exploring an empty repo until it trips
    # the recursion limit — mirrors test_llm_live.test_live_review_ticket.
    (rebar_repo / "app.py").write_text("API_KEY = 'hardcoded-secret'\n", encoding="utf-8")

    # ``review_code`` supplies a findings-returning operation that exercises the traced
    # four-pass model path. The assertion only requires a completed review result.
    diff = "--- a/app.py\n+++ b/app.py\n@@ -0,0 +1 @@\n+API_KEY = 'hardcoded-secret'\n"

    # from_env() picks up the LANGFUSE_* creds so tracing is enabled for this run.
    cfg = LLMConfig.from_env(repo_root=str(rebar_repo))
    cfg.model = _MODEL
    assert cfg.langfuse.enabled, "LANGFUSE_* must be configured for this test"

    # The timestamp only bounds the query. A snapshot of existing IDs requires a new trace, so
    # stale data cannot satisfy the assertion. The skew margin widens the query window.
    from_ts = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=_SKEW_MARGIN_S)
    query = urllib.parse.urlencode(
        {"fromTimestamp": from_ts.strftime("%Y-%m-%dT%H:%M:%SZ"), "limit": 100}
    )

    def _trace_ids() -> set[str]:
        listing = _lf_get(f"/api/public/traces?{query}")
        return {e["id"] for e in (listing or {}).get("data", [])}

    seen_before = _trace_ids()

    result = llm.review_code(
        diff_text=diff,
        changed_files=["app.py"],
        target_ticket=epic,
        repo_root=str(rebar_repo),
        config=cfg,
    )
    assert result.get("findings") is not None, "review must have produced a result"

    # Push the batched spans to Langfuse's OTLP endpoint immediately.
    _force_flush_tracing()

    # Poll until a NEW trace (not present before this run) with observations is queryable.
    # Ingestion is async: the trace ROW can land before its OBSERVATION rows finish, so
    # keep polling until observations appear too rather than asserting on the first hit.
    deadline = time.monotonic() + _READ_TIMEOUT_S
    new_trace = None
    while time.monotonic() < deadline:
        for tid in _trace_ids() - seen_before:
            fetched = _lf_get(f"/api/public/traces/{tid}")
            if fetched and fetched.get("observations"):
                new_trace = fetched
                break
        if new_trace is not None:
            break
        time.sleep(_READ_INTERVAL_S)

    assert new_trace is not None, (
        "the live review run did not export a NEW OTLP trace with observations to Langfuse "
        f"within {_READ_TIMEOUT_S}s — the setup_tracing/OTLP export path emitted no spans"
    )
    # The exported trace must carry the agent/LLM/tool spans as observations.
    assert new_trace.get("observations"), (
        f"trace {new_trace.get('id')} ingested but carried no observations"
    )
