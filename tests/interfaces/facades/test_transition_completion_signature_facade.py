"""The completion-signature marker survives to the MCP surface (bug silvern-dewy-damselfly).

A close COMMITS and releases the lock BEFORE it attempts to sign, so a signing failure leaves
the ticket closed-WITHOUT-signature while the command still succeeds. `close_ticket` now
reports that in a `completion_signature` block — but the payload is rebuilt field by field on
its way out (`_lib_writes.transition`, and the CLI's json branch), and `transition_ticket`
forwards the LIBRARY result. So the marker reaching an MCP agent is a property of that chain,
not of `close_ticket` alone, and it is asserted here rather than assumed.

Offline: the signing call is monkeypatched to raise, so no key material and no lock contention
is involved.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

import rebar
from rebar import signing as _signing
from rebar._commands import transition_close


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    for args in (
        ("init", "-q"),
        ("config", "user.email", "test@example.com"),
        ("config", "user.name", "Test"),
    ):
        subprocess.run(["git", *args], cwd=r, check=True)
    monkeypatch.setenv("REBAR_ROOT", str(r))
    rebar.init_repo(repo_root=str(r))
    return r


def _call_transition(ticket_id: str, current: str, target: str) -> dict:
    pytest.importorskip("mcp")
    from adapters import _unwrap  # tests/interfaces on sys.path

    from rebar.mcp_server import build_server

    srv = build_server()
    return _unwrap(
        asyncio.run(
            srv.call_tool(
                "transition_ticket",
                {"ticket_id": ticket_id, "current_status": current, "target_status": target},
            )
        )
    )


def test_mcp_transition_carries_the_lost_signature_marker(repo: Path, monkeypatch):
    """The MCP half of the defect: an agent must be able to see, WITHOUT parsing English off
    a stderr stream it never receives, that the close LANDED but its signature did not."""
    monkeypatch.setattr(
        transition_close,
        "_completion_precheck",
        lambda *a, **k: ({"verdict": "PASS", "findings": []}, "required"),
    )
    monkeypatch.setattr(transition_close, "_material_drifted", lambda *_a: False)

    def _boom(*_a, **_kw):
        raise _signing.SigningError("flock: could not acquire lock after 60s")

    monkeypatch.setattr(transition_close, "sign_completion_verdict", _boom)
    tid = rebar.create_ticket("task", "a task", repo_root=str(repo))
    rebar.transition(tid, "open", "in_progress", repo_root=str(repo))
    monkeypatch.chdir(repo)

    result = _call_transition(tid, "in_progress", "closed")

    assert result["completion_signature"]["signed"] is False
    assert result["completion_signature"]["cause"] == "sign_failed"
    assert rebar.show_ticket(tid, repo_root=str(repo))["status"] == "closed"


def test_mcp_transition_without_a_close_carries_no_marker(repo: Path, monkeypatch):
    """Absence is meaningful and must reach MCP too: no key means "not a completion close",
    so an agent branching on it is not misled by a plain status move."""
    tid = rebar.create_ticket("task", "a task", repo_root=str(repo))
    monkeypatch.chdir(repo)

    result = _call_transition(tid, "open", "in_progress")

    assert "completion_signature" not in result
