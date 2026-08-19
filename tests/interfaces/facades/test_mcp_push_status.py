"""An MCP write tool must tell its caller the ticket events did not reach the remote
(bug vapoury-attack-lamb).

The MCP limb of this bug was originally STATIC evidence. It was confirmed end-to-end
first: with a real bare origin whose real ``pre-receive`` hook declined, the
``comment_ticket`` tool returned

    ([TextContent(text='ok')], {'result': 'ok'})

while two ticket commits sat stranded locally. The server DOES install a stderr log
handler, but it writes to the SERVER's stderr — the writing agent reads only the tool
result, so on this surface the warning is undeliverable, not merely uninformative.

These tests drive the real in-process MCP server (``build_server().call_tool(...)``) — the
same path an agent takes — against a real declining origin. Nothing is mocked.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

import rebar

pytestmark = pytest.mark.unit

_DECLINE_HOOK = """\
#!/bin/sh
echo "remote: error: GH013: Repository rule violations found for refs/heads/tickets." >&2
echo "remote: - Push cannot contain secrets" >&2
exit 1
"""


def _git(d: Path, *a: str) -> subprocess.CompletedProcess[str]:
    r = subprocess.run(["git", "-C", str(d), *a], capture_output=True, text=True)
    assert r.returncode == 0, f"git {' '.join(a)} failed: {r.stderr}"
    return r


def _bare_git(d: Path, *a: str) -> subprocess.CompletedProcess[str]:
    r = subprocess.run(["git", "--git-dir", str(d), *a], capture_output=True, text=True)
    assert r.returncode == 0, f"git {' '.join(a)} failed: {r.stderr}"
    return r


@pytest.fixture
def declining_store(rebar_repo: Path, tmp_path: Path) -> Path:
    """Repoint the store's tracker at an origin that declines every push."""
    tracker = rebar_repo / ".tickets-tracker"
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(tracker), "remote", "remove", "origin"], capture_output=True, text=True
    )
    _git(tracker, "remote", "add", "origin", str(origin))
    _git(tracker, "push", "-q", "origin", "HEAD:tickets")
    _git(tracker, "fetch", "-q", "origin", "+refs/heads/tickets:refs/remotes/origin/tickets")
    hook = origin / "hooks" / "pre-receive"
    hook.write_text(_DECLINE_HOOK)
    hook.chmod(0o755)
    _bare_git(origin, "config", "core.hooksPath", str(origin / "hooks"))
    return tracker


def _call(tool: str, **args: object) -> dict:
    """The payload a client actually reads back from ``tool``.

    FastMCP returns ``(content_blocks, structured)`` for a tool that advertises an
    outputSchema, and a bare content list for one that does not (``transition_ticket`` /
    ``reopen_ticket``, whose ``from`` key is a Python reserved word). Both shapes are
    normalized here so the assertions are about the DATA a caller sees, not the wrapper.
    """
    import json

    from rebar.mcp_server import build_server

    result = asyncio.run(build_server().call_tool(tool, args))
    if isinstance(result, tuple):
        return result[1]
    return json.loads(result[0].text)


def test_comment_tool_reports_a_rejected_push(declining_store: Path) -> None:
    """The measured case. Before the fix this returned exactly ``{'result': 'ok'}``."""
    tid = rebar.create_ticket("task", "subject of the outage")

    out = _call("comment_ticket", ticket_id=tid, body="written during the outage")

    assert out.get("result") == "ok", (
        f"the pre-existing ack must be preserved (superset, not replacement): {out}"
    )
    status = out.get("push_status")
    assert status, f"the MCP write result carries no push status at all: {out}"
    assert status["state"] == "pending", (
        "the tool reported success while the push was really declined and the ticket "
        f"events are stranded locally. Got: {status}"
    )
    assert "GH013" in (status.get("detail") or "") or "declined" in (status.get("detail") or "")


def test_a_healthy_store_reports_ok_rather_than_nothing(rebar_repo: Path) -> None:
    """The field must be present and explicit on the happy path too.

    An absent field is indistinguishable from an old server, so a client could not tell
    "delivered" from "this server cannot tell me". ``rebar_repo``'s tracker has no
    reachable remote configured, which is the supported local-only mode: nothing failed, so
    nothing is pending.
    """
    tid = rebar.create_ticket("task", "healthy")
    out = _call("comment_ticket", ticket_id=tid, body="ordinary")
    assert out.get("push_status", {}).get("state") == "ok", out


def test_the_structured_write_results_carry_it_too(declining_store: Path) -> None:
    """create/claim return typed models, not the shared ack — they must carry it as well.

    Their schemas are mirrored under ``src/rebar/schemas`` and guarded by a drift gate, so
    this pins that the new field survives model validation rather than being dropped.
    """
    created = _call("create_ticket", ticket_type="task", title="typed result")
    assert created.get("push_status", {}).get("state") == "pending", created
    assert created.get("id"), "the pre-existing create shape was lost"

    claimed = _call("claim_ticket", ticket_id=created["id"])
    assert claimed.get("push_status", {}).get("state") == "pending", claimed
    assert claimed.get("status") == "in_progress", "the pre-existing claim shape was lost"


def test_the_schemaless_dict_tools_carry_it(declining_store: Path) -> None:
    """transition/reopen return plain dicts (the ``from`` reserved word); same guarantee."""
    tid = rebar.create_ticket("task", "dict result")
    rebar.claim(tid)
    out = _call(
        "transition_ticket", ticket_id=tid, current_status="in_progress", target_status="closed"
    )
    assert out.get("push_status", {}).get("state") == "pending", out
    assert out.get("to") == "closed", "the pre-existing transition shape was lost"


def test_every_write_tool_advertising_a_schema_declares_push_status(rebar_repo: Path) -> None:
    """Coverage is sourced MECHANICALLY from the server, so a new write tool cannot
    silently ship without the field.

    A hand-written list would rot; this enumerates the registered write tools from
    ``list_tools()`` and checks their advertised ``outputSchema``. The one documented
    exception is ``declare_no_file_impact``, which deliberately uses FastMCP's
    unstructured-output mode and therefore advertises no schema to add a field to.
    """
    from rebar.mcp_server import build_server

    srv = build_server()
    tools = {t.name: t for t in asyncio.run(srv.list_tools())}
    write_tools = {
        "comment_ticket",
        "edit_ticket",
        "link_tickets",
        "unlink_tickets",
        "tag_ticket",
        "untag_ticket",
        "archive_ticket",
        "compact_ticket",
        "set_file_impact",
        "set_verify_commands",
        "create_ticket",
        "create_idea",
        "create_identity",
        "log_session",
        "claim_ticket",
        "sign_manifest",
    }
    missing = []
    for name in sorted(write_tools):
        assert name in tools, f"{name} is not registered; update this list"
        schema = tools[name].outputSchema or {}
        text = repr(schema)
        if "push_status" not in text:
            missing.append(name)
    assert not missing, f"write tools advertise no push_status in their outputSchema: {missing}"
