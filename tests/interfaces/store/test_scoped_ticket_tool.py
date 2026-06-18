"""WS-D3: scoped rebar ticket tool (read + comment only) + model precedence.

The scoped tool needs langchain_core (@tool); the precedence resolver is pure.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import rebar
from rebar.llm import runner
from rebar.llm.config import DEFAULT_MODEL, LLMConfig, resolve_model


def test_resolve_model_precedence() -> None:
    cfg = LLMConfig()  # cfg.model = env REBAR_LLM_MODEL or DEFAULT_MODEL
    assert resolve_model(cfg, step="openai:gpt-4o", workflow="anthropic:x") == "openai:gpt-4o"
    assert resolve_model(cfg, workflow="anthropic:claude-3") == "anthropic:claude-3"
    assert resolve_model(cfg) == cfg.model
    assert resolve_model(LLMConfig(model="m")) == "m"
    # default folds through when nothing set
    assert DEFAULT_MODEL  # sanity: the default exists


def test_scoped_tools_are_read_plus_comment_only() -> None:
    pytest.importorskip("langchain_core")
    tools = runner._scoped_ticket_tools(repo_path=None, allow_comment=True)
    names = {t.name for t in tools}
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


def test_scoped_tools_readonly_withholds_comment(monkeypatch) -> None:
    pytest.importorskip("langchain_core")
    monkeypatch.setenv("REBAR_MCP_READONLY", "1")
    tools = runner._scoped_ticket_tools(repo_path=None)  # allow_comment inferred from gate
    names = {t.name for t in tools}
    assert names == {"show_ticket"}  # comment withheld under readonly


def test_show_ticket_tool_reads(rebar_repo: Path) -> None:
    pytest.importorskip("langchain_core")
    tid = rebar.create_ticket("task", "Scoped Read", repo_root=str(rebar_repo))
    tools = {t.name: t for t in runner._scoped_ticket_tools(repo_path=str(rebar_repo))}
    out = tools["show_ticket"].invoke({"ticket_id": tid})
    assert "Scoped Read" in out and tid in out


def test_comment_ticket_tool_comments(rebar_repo: Path) -> None:
    pytest.importorskip("langchain_core")
    tid = rebar.create_ticket("task", "Scoped Comment", repo_root=str(rebar_repo))
    tools = {t.name: t for t in runner._scoped_ticket_tools(repo_path=str(rebar_repo))}
    msg = tools["comment_ticket"].invoke({"ticket_id": tid, "body": "agent note"})
    assert "Commented" in msg
    state = rebar.show_ticket(tid, repo_root=str(rebar_repo))
    assert any("agent note" in (c.get("body") or "") for c in state["comments"])
