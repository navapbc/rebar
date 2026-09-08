"""Bedrock prompt-cache canary for the external tier.

The test requires both external and cache-canary opt-ins plus credentials from the boto3 chain.
It repeats a cache-eligible system prefix on the configured Bedrock model and requires
``cache_read_tokens`` on the second response. This detects regressions in provider cache wiring
and capability selection.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.external

_skip = pytest.mark.skipif(
    os.environ.get("REBAR_LIVE_BEDROCK", "").strip().lower() not in ("1", "true", "yes"),
    reason="set REBAR_LIVE_BEDROCK=1 (plus ambient AWS credentials) to run the live Bedrock "
    "cache canary",
)

# A large, IDENTICAL-across-both-calls system prompt so it clears the provider's minimum
# cacheable-prefix size (Claude's cache-write floor is on the order of ~1KB of tokens) — a
# short prompt would never trigger a cache write/read regardless of wiring correctness.
_CACHE_PADDING = (
    "You are a meticulous, detail-oriented code reviewer for the rebar ticket system. "
    "rebar is an event-sourced ticket tracker with a git-backed store, exposed as a Python "
    "library, a CLI, and an MCP server. When reviewing, consider correctness, readability, "
    "architecture, security, and performance. Always cite specific evidence for any finding "
    "you report, and never speculate about code you have not read. "
) * 20


def _cfg(repo: Path):
    from rebar.llm.bedrock_model import DEFAULT_BEDROCK_MODEL_ID
    from rebar.llm.config import LLMConfig

    return LLMConfig(
        model=f"bedrock:{DEFAULT_BEDROCK_MODEL_ID}",
        repo_path=str(repo),
        runner="pydantic_ai",
    )


@_skip
def test_bedrock_repeated_prefix_hits_cache(tmp_path: Path) -> None:
    """Two identical-prefix single-turn calls: the SECOND must report a real cache hit."""
    from rebar.llm.runner import PydanticAIRunner, RunRequest

    cfg = _cfg(tmp_path)
    runner = PydanticAIRunner(cfg)
    runner.preflight()

    def _call() -> dict:
        req = RunRequest(
            system_prompt=_CACHE_PADDING,
            instructions="Reply with exactly the word: ready",
            config=cfg,
            mode="text",
            reviewers=[],
            # No filesystem/rebar tools needed for a plain text reply — single_turn is the
            # faithful minimal exercise of the cache-instructions path (mirrors
            # test_pydantic_ai_cutover_live.py::test_pydantic_text_mode).
            execution_mode="single_turn",
        )
        return runner.run(req)

    first = _call()
    assert first["runner"] == "pydantic_ai"

    second = _call()
    usage = second.get("_usage") or {}
    assert usage.get("cache_read_tokens", 0) > 0, (
        f"expected a cache HIT on the second identical-prefix call, got usage={usage!r} — "
        "either the prefix did not clear the provider's cache-write floor, or Bedrock "
        "caching regressed for this model"
    )
