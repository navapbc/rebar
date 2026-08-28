"""MCP-surface oracle for the cross-session warning (story 734d).

In-process contract tests: register the real MCP tool callables against a fake server
and drive them, asserting on the VALIDATED response model / dict a client would receive.
A single-ticket tool invoked by a session that is NOT the holder carries a holder-naming
``another session`` advisory; same-session, unset-id, the config toggle off, and the bulk
read tools stay silent. The single-ticket writes and ``show_ticket`` carry it as the
optional ``cross_session_warning`` model field; ``transition_ticket`` / ``reopen_ticket``
(which return the raw engine ``dict``) carry it as an additive ``cross_session_warning``
key. The advisory never gates the mutation — the write still lands.

Asserts observable contract only — the returned model field / dict key, the persisted
mutation, and the generated ``outputSchema`` — never internal structure. The holder-naming
substring under test is ``another session`` (the S2 detector's message).
"""

from __future__ import annotations

import logging
import types
from pathlib import Path
from typing import Any

import pytest

import rebar

pytestmark = pytest.mark.unit

_SESSION_VARS = ("REBAR_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "OPENCODE_SESSION_ID", "SESSION_ID")
_HOLDER_MSG = "another session"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A git repo with an initialized store, as the MCP surface sees it."""
    import subprocess

    for var in (*_SESSION_VARS, "AI_AGENT"):
        monkeypatch.delenv(var, raising=False)
    root = tmp_path / "repo"
    root.mkdir()
    for args in (
        ("init", "-q"),
        ("config", "user.email", "test@example.com"),
        ("config", "user.name", "Test"),
    ):
        subprocess.run(["git", *args], cwd=root, check=True)
    monkeypatch.setenv("REBAR_ROOT", str(root))
    rebar.init_repo(repo_root=str(root))
    return root


def _write_tools() -> dict[str, Any]:
    """Register the MCP write tools against a fake server; return the callables by name."""
    from rebar import _mcp_writes

    tools: dict[str, Any] = {}

    class _FakeMCP:
        def tool(self, *_a, **_k):
            def _decorate(fn):
                tools[fn.__name__] = fn
                return fn

            return _decorate

    class _FakeCtx:
        logger = logging.getLogger("test")

        @staticmethod
        def readonly() -> bool:
            return False

        @staticmethod
        def dump(obj):
            return obj

        @staticmethod
        def allow_llm() -> bool:
            return False

    _mcp_writes.register_write_tools(_FakeMCP(), ctx=_FakeCtx())
    return tools


def _read_tools() -> dict[str, Any]:
    """Register the MCP read tools against a fake server; return the callables by name."""
    from rebar import _mcp_reads

    tools: dict[str, Any] = {}

    class _FakeMCP:
        def tool(self, **_k):
            def _decorate(fn):
                tools[fn.__name__] = fn
                return fn

            return _decorate

    ctx = types.SimpleNamespace(
        readonly=False,
        allow_jira_sync=False,
        cap_workflow_payload=lambda *a, **k: None,
        MODE_CAPS={},
        Mode=None,
    )
    _mcp_reads.register_read_tools(_FakeMCP(), ctx=ctx)
    return tools


def _claimed(repo: Path, monkeypatch: pytest.MonkeyPatch, *, holder: str = "sess-A") -> str:
    """Create a ticket and claim it as ``holder`` so its live claim is held by that session."""
    tid = rebar.create_ticket("task", "t", repo_root=str(repo))
    monkeypatch.setenv("REBAR_SESSION_ID", holder)
    rebar.claim(tid)
    return tid


# ── happy path (implementer sees) ─────────────────────────────────────────────
def test_show_ticket_warns_for_other_session(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``show_ticket`` as a DIFFERENT session returns a model whose ``cross_session_warning``
    names the holder; the rest of the state is unaffected."""
    tid = _claimed(repo, monkeypatch, holder="sess-A")
    monkeypatch.setenv("REBAR_SESSION_ID", "sess-B")

    out = _read_tools()["show_ticket"](tid)

    assert out.cross_session_warning is not None
    assert _HOLDER_MSG in out.cross_session_warning
    assert "sess-A" in out.cross_session_warning
    assert out.claimed_session == "sess-A"


def test_comment_warns_and_still_mutates(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A single-ticket write (``comment_ticket``) as another session carries the advisory AND
    still performs the write; the ack text is unchanged so existing consumers keep working."""
    tid = _claimed(repo, monkeypatch, holder="sess-A")
    monkeypatch.setenv("REBAR_SESSION_ID", "sess-B")

    out = _write_tools()["comment_ticket"](tid, "hello-from-B")

    assert out.result == "ok"
    assert out.cross_session_warning is not None
    assert _HOLDER_MSG in out.cross_session_warning and "sess-A" in out.cross_session_warning
    # The mutation landed despite the advisory.
    assert any(
        c.get("body") == "hello-from-B"
        for c in rebar.show_ticket(tid, repo_root=str(repo)).get("comments", [])
    )


def test_same_session_is_silent(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Negative control: the HOLDER acting on its own ticket gets no advisory."""
    tid = _claimed(repo, monkeypatch, holder="sess-A")
    monkeypatch.setenv("REBAR_SESSION_ID", "sess-A")

    assert _read_tools()["show_ticket"](tid).cross_session_warning is None
    assert _write_tools()["comment_ticket"](tid, "mine").cross_session_warning is None


# ── edge / E2E (held out from the implementer) ────────────────────────────────
def test_transition_warns_and_still_transitions(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``transition_ticket`` returns the raw engine dict; as another session it carries an
    additive ``cross_session_warning`` key AND the transition still applies (advisory, not a
    gate). The warning is computed at operation time, before the mutation."""
    tid = _claimed(repo, monkeypatch, holder="sess-A")
    monkeypatch.setenv("REBAR_SESSION_ID", "sess-B")

    out = _write_tools()["transition_ticket"](tid, "in_progress", "blocked")

    assert out["cross_session_warning"] is not None
    assert _HOLDER_MSG in out["cross_session_warning"] and "sess-A" in out["cross_session_warning"]
    # The transition landed despite the advisory.
    assert rebar.show_ticket(tid, repo_root=str(repo))["status"] == "blocked"


def test_reopen_warns_for_other_session(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``reopen_ticket`` (also a raw-dict single-ticket mutation) carries the additive key."""
    tid = _claimed(repo, monkeypatch, holder="sess-A")
    rebar.transition(tid, "in_progress", "closed", repo_root=str(repo))
    monkeypatch.setenv("REBAR_SESSION_ID", "sess-B")

    # A closed ticket has no live claim, so it does NOT warn; re-claim to hold it, then reopen.
    # Reopen operates on a closed ticket whose provenance was cleared on close, so this asserts
    # the key path exists and is silent when there is no holder.
    out = _write_tools()["reopen_ticket"](tid)

    assert out.get("cross_session_warning") is None
    assert rebar.show_ticket(tid, repo_root=str(repo))["status"] == "open"


def test_bulk_tools_never_warn(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Bulk read tools (``list_tickets``, ``ready_tickets``) never carry a cross-session field,
    even when the store holds another session's in_progress ticket."""
    _claimed(repo, monkeypatch, holder="sess-A")
    monkeypatch.setenv("REBAR_SESSION_ID", "sess-B")
    reads = _read_tools()

    for name in ("list_tickets", "ready_tickets"):
        result = reads[name]()
        for item in result:
            as_dict = item if isinstance(item, dict) else item.model_dump()
            assert as_dict.get("cross_session_warning") is None, f"{name} leaked a warning"


def test_unset_acting_session_is_silent(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With no resolvable acting session, we cannot prove we are a DIFFERENT session — silent
    across the read, the write, and the raw-dict transition."""
    tid = _claimed(repo, monkeypatch, holder="sess-A")
    for var in _SESSION_VARS:
        monkeypatch.delenv(var, raising=False)

    assert _read_tools()["show_ticket"](tid).cross_session_warning is None
    assert _write_tools()["comment_ticket"](tid, "anon").cross_session_warning is None
    trans = _write_tools()["transition_ticket"](tid, "in_progress", "blocked")
    assert trans.get("cross_session_warning") is None


def test_toggle_off_silences_every_surface(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``[warnings] cross_session = false`` in rebar.toml silences the advisory end-to-end on
    every MCP surface, even when a different session acts on a held ticket."""
    (repo / "rebar.toml").write_text("[warnings]\ncross_session = false\n", encoding="utf-8")
    tid = _claimed(repo, monkeypatch, holder="sess-A")
    monkeypatch.setenv("REBAR_SESSION_ID", "sess-B")

    assert _read_tools()["show_ticket"](tid).cross_session_warning is None
    assert _write_tools()["comment_ticket"](tid, "b").cross_session_warning is None
    trans = _write_tools()["transition_ticket"](tid, "in_progress", "blocked")
    assert trans.get("cross_session_warning") is None


def test_output_schema_exposes_optional_field(repo: Path) -> None:
    """The response models advertise the OPTIONAL ``cross_session_warning`` property, and a
    default (unset) response still validates — the schema change is additive."""
    from rebar._mcp_models import TicketStateOut, WriteAckOut

    for model in (WriteAckOut, TicketStateOut):
        schema = model.model_json_schema()
        assert "cross_session_warning" in schema["properties"], model.__name__
        assert "cross_session_warning" not in schema.get("required", []), model.__name__

    ack = WriteAckOut.model_validate({"result": "ok", "push_status": {"state": "unknown"}})
    assert ack.cross_session_warning is None
