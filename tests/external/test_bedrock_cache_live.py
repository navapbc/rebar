"""LIVE Bedrock prompt-cache canary (story S3, ticket 2932).

Marked ``external`` -> inert in the default suite (see ``tests/external/conftest.py``); runs
only with ``REBAR_RUN_EXTERNAL=1`` (the tier opt-in) **and** ``REBAR_LIVE_BEDROCK=1`` (this
canary's own opt-in, mirroring how ``test_completion_pass_live.py`` additionally gates on
``ANTHROPIC_API_KEY``) plus real ambient AWS credentials (instance role / ``AWS_PROFILE`` /
boto3's default chain — rebar manages no Bedrock key, see ``rebar.llm.bedrock_model``).

Proves the MEASURED, load-bearing fact this story's cache-effectiveness warning
(``rebar.llm.structured_run.warn_if_cache_ineffective``) exists to detect: on the DEFAULT
Bedrock model (``us.anthropic.claude-sonnet-4-6``), repeating an identical, large system
prompt (a cache-eligible prefix) actually gets a cache HIT on the second call
(``cache_read_tokens > 0``) — the positive case, kept alive so a regression in either
pydantic-ai's Bedrock cache wiring or rebar's ``capabilities.py``/``providers.py`` seam would
be caught by this canary rather than only ever discovered by an operator's silent bill.

Run::  REBAR_RUN_EXTERNAL=1 REBAR_LIVE_BEDROCK=1 pytest -m external \
           tests/external/test_bedrock_cache_live.py
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
    from rebar.llm.config import DEFAULT_BEDROCK_MODEL_ID, LLMConfig

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
