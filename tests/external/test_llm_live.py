"""Live-runtime validation of the langgraph runner (ticket b2e5).

The seed of rebar's **external-integration suite** (``tests/external/``): tests
that hit third-party services. These exercise the REAL agent path end-to-end — a
live, billable model call — so they are marked ``external`` (excluded from the
default CI run, which uses ``-m "not integration and not external"``) and skip
unless both an API key and the ``agents`` extra are present. They validate the
runtime behaviors that can't be checked offline:

  * the model call succeeds (in particular, no `temperature` is sent to
    claude-opus-4.x, which would 400),
  * ``structured_response`` is populated (no StructuredOutputError),
  * the output validates against the review_result schema, with model/runner
    provenance wired through and file citations resolved.

Run locally with credentials::

    ANTHROPIC_API_KEY=… pytest -m external tests/external

Langfuse trace delivery and MCP-server session lifecycle need their own live
services (LANGFUSE_* / REBAR_LLM_MCP_SERVERS) and are validated by configuring
those env vars before running; see docs/llm-framework.md.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import rebar
from rebar import schemas

pytestmark = pytest.mark.external


def _live_model() -> str | None:
    """The model to validate, based on which provider key is present (or None)."""
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
_skip = pytest.mark.skipif(_MODEL is None, reason="no LLM API key / agents extra")


@_skip
def test_live_review_ticket(rebar_repo: Path) -> None:
    import rebar.llm as llm
    from rebar.llm.config import LLMConfig

    epic = rebar.create_ticket(
        "epic",
        "Add login",
        description="Build login.\n\n## Acceptance Criteria\n- [ ] users can log in",
        repo_root=str(rebar_repo),
    )
    (rebar_repo / "app.py").write_text("API_KEY = 'hardcoded-secret'\n", encoding="utf-8")

    cfg = LLMConfig(model=_MODEL, repo_path=str(rebar_repo), max_iterations=15)
    result = llm.review_ticket(epic, "ticket-quality", repo_root=str(rebar_repo), config=cfg)

    schemas.validator(schemas.REVIEW_RESULT).validate(result)
    assert result["runner"] == "langgraph"
    assert result["model"] == _MODEL  # no temperature 400; the call went through
    assert isinstance(result["findings"], list)  # structured_response populated


@_skip
def test_live_review_code(rebar_repo: Path) -> None:
    import rebar.llm as llm
    from rebar.llm.config import LLMConfig

    diff = (
        "--- a/auth.py\n+++ b/auth.py\n@@ -0,0 +1,3 @@\n"
        "+def check(token):\n+    return True  # TODO: actually verify\n"
    )
    cfg = LLMConfig(model=_MODEL, repo_path=str(rebar_repo), max_iterations=15)
    result = llm.review_code(
        diff_text=diff,
        changed_files=["auth.py"],
        reviewers=["code-quality"],
        config=cfg,
    )
    schemas.validator(schemas.REVIEW_RESULT).validate(result)
    assert result["model"] == _MODEL
    assert isinstance(result["findings"], list)
