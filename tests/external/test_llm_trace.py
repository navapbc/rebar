"""Live Langfuse round-trip validation (ticket 9bd5): a real review run must emit
a trace that is fetchable back through the Langfuse API.

Runs against a SELF-HOSTED Langfuse (the ``docker-compose.langfuse.yml`` stack) —
locally or as the ephemeral stack the external-integration CI job brings up. Like
the other ``tests/external`` suites it is inert unless ``REBAR_RUN_EXTERNAL=1`` and
skips unless an LLM key + the ``agents`` extra + a configured Langfuse are all
present.

Why a REST round-trip (not the SDK's fetch helpers): the public
``GET /api/public/traces/{id}`` endpoint is stable across SDK majors, so this
dodges the SDK-v4-vs-self-hosted read-endpoint drift. Ingestion is async — the
trace is queryable only a few seconds AFTER ``flush()`` — so we poll with a
bounded read-retry loop (the same pattern Langfuse's own e2e tests use).
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import rebar

pytestmark = pytest.mark.external

_READ_TIMEOUT_S = 60.0  # ingestion is async; poll up to this long after flush()
_READ_INTERVAL_S = 1.0


def _live_model() -> str | None:
    try:
        import rebar.llm as llm
    except Exception:
        return None
    if not llm.agents_extra_installed():
        return None
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "claude-opus-4-8"
    if os.environ.get("OPENAI_API_KEY"):
        return "gpt-4o"
    return None


_MODEL = _live_model()
_LF_HOST = os.environ.get("LANGFUSE_HOST")
_LF_PK = os.environ.get("LANGFUSE_PUBLIC_KEY")
_LF_SK = os.environ.get("LANGFUSE_SECRET_KEY")
_lf_ready = bool(_MODEL and _LF_HOST and _LF_PK and _LF_SK)
_skip = pytest.mark.skipif(
    not _lf_ready,
    reason="needs an LLM key + agents extra + LANGFUSE_HOST/PUBLIC_KEY/SECRET_KEY",
)


def _fetch_trace(trace_id: str) -> dict | None:
    """GET /api/public/traces/{id} with HTTP Basic auth (public:secret). Returns the
    decoded trace dict, or None on 404 (not yet ingested) — raising on other errors."""
    url = f"{_LF_HOST.rstrip('/')}/api/public/traces/{trace_id}"
    token = base64.b64encode(f"{_LF_PK}:{_LF_SK}".encode()).decode()
    req = urllib.request.Request(url, headers={"Authorization": f"Basic {token}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 (trusted local host)
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None  # async ingestion lag — caller retries
        raise


@_skip
def test_live_review_emits_fetchable_langfuse_trace(rebar_repo: Path) -> None:
    import rebar.llm as llm
    from rebar.llm.config import LLMConfig

    epic = rebar.create_ticket(
        "epic",
        "Add login",
        description="Build login.\n\n## Acceptance Criteria\n- [ ] users can log in",
        repo_root=str(rebar_repo),
    )

    # from_env() picks up the LANGFUSE_* creds so tracing is enabled for this run.
    cfg = LLMConfig.from_env(repo_root=str(rebar_repo))
    cfg.model = _MODEL
    assert cfg.langfuse.enabled, "LANGFUSE_* must be configured for this test"

    result = llm.review_ticket(epic, "ticket-quality", repo_root=str(rebar_repo), config=cfg)
    trace_id = result.get("trace_id")
    assert trace_id, "review_result must carry a trace_id when Langfuse is enabled"

    # Poll the public API until the (async-ingested) trace is queryable. The trace
    # ROW can land before its OBSERVATION rows finish ingesting, so keep polling
    # until observations appear too (within the same deadline) rather than asserting
    # on the first hit — otherwise the observations check is racy.
    deadline = time.monotonic() + _READ_TIMEOUT_S
    trace = None
    while time.monotonic() < deadline:
        fetched = _fetch_trace(trace_id)
        if fetched is not None:
            trace = fetched
            if fetched.get("observations"):
                break  # fully ingested (trace + its observations)
        time.sleep(_READ_INTERVAL_S)

    assert trace is not None, f"trace {trace_id} never became fetchable within {_READ_TIMEOUT_S}s"
    assert trace.get("id") == trace_id
    # The review is wrapped in a `rebar.review` span (runner._trace), so the trace
    # must carry at least one observation from the agent run.
    assert trace.get("observations"), (
        f"trace {trace_id} fetched but no observations ingested within {_READ_TIMEOUT_S}s"
    )
