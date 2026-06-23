"""WS-D3: scoped rebar ticket tool (read + comment only) + model precedence.

The scoped tools are now the pydantic_ai runner's native plain-function ticket
tools (``pai_tools.rebar_tools``); the precedence resolver is pure.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import rebar
from rebar.llm.config import DEFAULT_MODEL, LLMConfig, resolve_model

pytest.importorskip("pydantic_ai")


def _tools(repo_path=None, *, allow_comment):
    from rebar.llm import pai_tools

    return {t.__name__: t for t in pai_tools.rebar_tools(repo_path, allow_comment=allow_comment)}


def test_resolve_model_precedence() -> None:
    cfg = LLMConfig()  # cfg.model = env REBAR_LLM_MODEL or DEFAULT_MODEL
    assert resolve_model(cfg, step="openai:gpt-4o", workflow="anthropic:x") == "openai:gpt-4o"
    assert resolve_model(cfg, workflow="anthropic:claude-3") == "anthropic:claude-3"
    assert resolve_model(cfg) == cfg.model
    assert resolve_model(LLMConfig(model="m")) == "m"
    # default folds through when nothing set
    assert DEFAULT_MODEL  # sanity: the default exists


def test_scoped_tools_are_read_plus_comment_only() -> None:
    names = set(_tools(allow_comment=True))
    assert names == {"show_ticket", "comment_ticket"}
    # The dangerous verbs must NOT be present.
    assert not (
        names
        & {
            "transition_ticket",
            "edit_ticket",
            "claim_ticket",
            "sign_manifest",
            "create_ticket",
            "link_tickets",
            "reopen_ticket",
        }
    )


def test_scoped_tools_readonly_withholds_comment() -> None:
    names = set(_tools(allow_comment=False))
    assert names == {"show_ticket"}  # comment withheld when not allowed


def test_show_ticket_tool_reads(rebar_repo: Path) -> None:
    tid = rebar.create_ticket("task", "Scoped Read", repo_root=str(rebar_repo))
    tools = _tools(str(rebar_repo), allow_comment=False)
    out = tools["show_ticket"](tid)
    assert "Scoped Read" in out and tid in out


def test_comment_ticket_tool_comments(rebar_repo: Path) -> None:
    tid = rebar.create_ticket("task", "Scoped Comment", repo_root=str(rebar_repo))
    tools = _tools(str(rebar_repo), allow_comment=True)
    msg = tools["comment_ticket"](tid, "agent note")
    assert "Commented" in msg
    state = rebar.show_ticket(tid, repo_root=str(rebar_repo))
    assert any("agent note" in (c.get("body") or "") for c in state["comments"])
