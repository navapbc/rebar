"""Unit tests for the shared session-id resolver (epic crust-fetch-stump, story 6014).

Covers the ordered precedence chain, empty-string handling, the all-absent -> None
contract (which also proves git HEAD is never returned), a source-guard that the two
former call sites no longer carry duplicate env-lookup chains, and a doc-content guard
that ``docs/config.md`` documents the unified contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rebar._commands.session_id import _SESSION_ID_VARS, resolve_session_id

_ALL_VARS = ("REBAR_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "SESSION_ID")


@pytest.fixture(autouse=True)
def _clear_session_vars(monkeypatch):
    """Start every test from a clean slate (the host may set these ambiently)."""
    for var in _ALL_VARS:
        monkeypatch.delenv(var, raising=False)


def test_rebar_session_id_wins(monkeypatch) -> None:
    """REBAR_SESSION_ID is authoritative even when the others are set."""
    monkeypatch.setenv("REBAR_SESSION_ID", "explicit-rebar")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "claude")
    monkeypatch.setenv("SESSION_ID", "ambient")
    assert resolve_session_id() == "explicit-rebar"


def test_claude_code_session_id_second(monkeypatch) -> None:
    """With REBAR unset, the native CLAUDE_CODE_SESSION_ID wins over ambient SESSION_ID."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "claude")
    monkeypatch.setenv("SESSION_ID", "ambient")
    assert resolve_session_id() == "claude"


def test_ambient_session_id_last(monkeypatch) -> None:
    """Only the ambient SESSION_ID set -> it is used."""
    monkeypatch.setenv("SESSION_ID", "ambient")
    assert resolve_session_id() == "ambient"


def test_all_absent_returns_none() -> None:
    """No var set -> None (and therefore never a git HEAD value)."""
    assert resolve_session_id() is None


def test_none_never_returns_head() -> None:
    """Explicit guard: the resolver's var list contains no git call, so an all-absent
    environment can only yield None, never a short-HEAD string."""
    assert resolve_session_id() is None
    assert not any(v.upper().startswith("GIT") for v in _SESSION_ID_VARS)


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_empty_or_whitespace_value_skipped(monkeypatch, blank) -> None:
    """An empty / whitespace-only higher-precedence var is skipped for the next one."""
    monkeypatch.setenv("REBAR_SESSION_ID", blank)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "claude")
    assert resolve_session_id() == "claude"


def test_empty_everywhere_returns_none(monkeypatch) -> None:
    """All vars empty -> treated as absent -> None."""
    for var in _ALL_VARS:
        monkeypatch.setenv(var, "  ")
    assert resolve_session_id() is None


def _src(rel: str) -> str:
    root = Path(__file__).resolve().parents[2]
    return (root / rel).read_text(encoding="utf-8")


def test_no_duplicate_env_chains_in_call_sites() -> None:
    """The two former resolvers delegate to the shared helper and carry NO duplicate
    ``os.environ.get("<session var>")`` chains — the only session-var env reads live in
    ``session_id.py``."""
    for rel in (
        "src/rebar/_commands/session_log.py",
        "src/rebar/_commands/transition_close.py",
    ):
        text = _src(rel)
        assert "resolve_session_id" in text, f"{rel} must delegate to the shared helper"
        for var in _ALL_VARS:
            assert f'os.environ.get("{var}"' not in text, (
                f"{rel} still contains a duplicate env-lookup for {var}"
            )


def test_docs_document_unified_contract() -> None:
    """docs/config.md documents the unified ordered contract (incl. CLAUDE_CODE_SESSION_ID)
    and no longer states the stale resolver contract as the session-id precedence."""
    doc = _src("docs/config.md")
    assert "CLAUDE_CODE_SESSION_ID" in doc
    assert "REBAR_SESSION_ID` → `CLAUDE_CODE_SESSION_ID` → `SESSION_ID`" in doc
    # The stale contract (SESSION_ID directly after REBAR_SESSION_ID, then short HEAD as
    # the *resolver* precedence) must no longer be presented as the session-id chain.
    assert "`REBAR_SESSION_ID` → `SESSION_ID` → short git HEAD" not in doc
