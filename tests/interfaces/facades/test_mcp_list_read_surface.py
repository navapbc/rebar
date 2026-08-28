"""The lean discovery row must actually be lean (story 98b8-5f08-1569-45cc).

``list_tickets``' docstring has always promised a lean default that omits the bulky
fields, but the lean path dropped only ``description``/``comments``. On the real store
(2,855 tickets) that left ``authorship_ledger`` (26.2 MB, 59% of the lean payload),
``attestations`` (10.4 MB, 23%) and ``signature`` (2.6 MB, 6%) in every row -- **88% of a
"lean" list's bytes were base64 signature material no list caller consumes**. Per row:
15,548 bytes vs 1,726 bytes.

A list caller is choosing a ticket; the signature record is read per-ticket via ``show`` /
``verify-signature``, which are untouched on every surface. So the drop set widens, and it
is spelled ONCE (``rebar._engine_support.reads.lean_projection``) for both read backends.

These tests drive the REGISTERED ``CallToolRequest`` handler a real stdio/HTTP client
reaches -- not just ``call_tool`` -- so a projection applied only to the post-
``build_server()`` instance cannot pass them.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

import rebar

pytestmark = pytest.mark.unit

#: The four fields that carry a list's signature bulk. None is DECLARED on
#: ``TicketStateOut``; they ride ``extra="allow"``, so dropping them from the state dict
#: removes them from the wire without touching the output schema -- which is why
#: ``full=True`` can return exactly the previous bytes.
SIGNATURE_BULK = ("authorship_ledger", "attestations", "signature", "keyring")


def _rows(structured: Any) -> list[dict]:
    """The ticket rows out of a FastMCP structured result (list tools wrap in `result`)."""
    if isinstance(structured, dict) and "result" in structured:
        return structured["result"]
    return structured


def _call(tool: str, **args: object) -> Any:
    """Call ``tool`` through the REGISTERED handler a real stdio/HTTP client reaches."""
    import mcp.types as types

    from rebar.mcp_server import build_server

    server = build_server()
    handler = server._mcp_server.request_handlers[types.CallToolRequest]
    request = types.CallToolRequest(
        method="tools/call", params=types.CallToolRequestParams(name=tool, arguments=dict(args))
    )
    return asyncio.run(handler(request)).root


def _bulky_state(index: int, *, ledger_entries: int = 1) -> dict:
    """A reduced ticket state carrying realistic signature bulk.

    Shaped like the real thing: a base64 DSSE envelope per ledger entry, which is what
    makes a production row 15 KB instead of 1.7 KB.
    """
    envelope = "A" * 2400  # ~ one base64 SSH-signature DSSE envelope
    return {
        "ticket_id": f"0000-0000-0000-{index:04x}",
        "ticket_type": "task",
        "title": f"ticket {index}",
        "status": "open",
        "priority": 2,
        "tags": [],
        "description": "body text",
        "comments": [{"body": "a comment"}],
        "authorship_ledger": [
            {"signature": envelope, "signer_pubkey": "ssh-ed25519 " + "B" * 68}
            for _ in range(ledger_entries)
        ],
        "attestations": {"plan-review": {"envelope": envelope}},
        "signature": {"manifest": ["step"], "envelope": envelope},
        "keyring": [{"pubkey": "ssh-ed25519 " + "B" * 68}],
    }


@pytest.fixture
def bulky_store(monkeypatch: pytest.MonkeyPatch):
    """Make the reducer yield ``count`` bulky states, so the REAL lean path runs on them."""

    def _install(count: int) -> None:
        import rebar._engine_support.reads as reads_mod

        monkeypatch.setattr(
            reads_mod,
            "reduce_all_tickets",
            lambda *a, **k: [_bulky_state(i) for i in range(count)],
        )

    return _install


def test_lean_list_omits_the_signature_bulk(rebar_repo: Path, bulky_store) -> None:
    """The lean default must drop the fields that ARE the bulk, not just the bodies."""
    bulky_store(3)
    rows = _rows(_call("list_tickets").structuredContent)
    assert len(rows) == 3
    present = {field for row in rows for field in SIGNATURE_BULK if row.get(field)}
    assert not present, (
        f"lean list_tickets still carries signature bulk: {sorted(present)}. "
        "These are 88% of a lean row's bytes on the real store."
    )


def test_full_list_still_carries_the_signature_bulk(rebar_repo: Path, bulky_store) -> None:
    """`full=True` is the opt-out: the drop must be a projection, not a schema removal."""
    bulky_store(3)
    rows = _rows(_call("list_tickets", full=True).structuredContent)
    for field in SIGNATURE_BULK:
        assert all(row.get(field) for row in rows), f"full=True dropped {field}"


def test_lean_row_is_an_order_of_magnitude_smaller(rebar_repo: Path, bulky_store) -> None:
    """Quantified: the lean projection must actually shrink the payload, not shave it."""
    bulky_store(3)
    lean = len(json.dumps(_rows(_call("list_tickets").structuredContent), default=str))
    full = len(json.dumps(_rows(_call("list_tickets", full=True).structuredContent), default=str))
    assert lean * 5 < full, f"lean={lean} full={full}: lean is not materially smaller"


def test_ready_tickets_is_lean_by_default_with_full_as_the_opt_out(
    rebar_repo: Path, monkeypatch
) -> None:
    """`ready_tickets` is the other whole-shape discovery surface, and it was the FULL one.

    It had no ``full`` flag at all, so this narrows a published default. Both directions are
    asserted here because that is what makes the narrowing a *projection*: the lean call
    must drop the bulk, and ``full=True`` must still carry every one of the four fields --
    an opt-out that did not restore them would be a schema removal wearing a flag.
    """
    rows = [_bulky_state(i) for i in range(3)]
    monkeypatch.setattr(rebar, "ready", lambda **_: list(rows))

    lean = _rows(_call("ready_tickets").structuredContent)
    present = {field for row in lean for field in SIGNATURE_BULK if row.get(field)}
    assert not present, f"lean ready_tickets still carries signature bulk: {sorted(present)}"

    full = _rows(_call("ready_tickets", full=True).structuredContent)
    for field in SIGNATURE_BULK:
        assert all(row.get(field) for row in full), f"ready_tickets full=True dropped {field}"


def test_both_discovery_surfaces_agree_on_their_default_shape(
    rebar_repo: Path, bulky_store, monkeypatch
) -> None:
    """The point of ONE shared projection: the two discovery tools cannot drift apart.

    ``list_tickets`` projects in the read core (which owns ``include_body``) and
    ``ready_tickets`` projects in the MCP tool layer (the library has no such flag), so
    they reach ``lean_projection`` by different routes. Sharing a function is only a claim
    until something compares the two outputs on the same rows.
    """
    states = [_bulky_state(i) for i in range(3)]
    bulky_store(3)
    monkeypatch.setattr(rebar, "ready", lambda **_: [dict(s) for s in states])

    listed = _rows(_call("list_tickets").structuredContent)
    ready = _rows(_call("ready_tickets").structuredContent)

    assert sorted(listed[0]) == sorted(ready[0]), (
        "the two discovery surfaces have drifted apart: "
        f"list-only={sorted(set(listed[0]) - set(ready[0]))} "
        f"ready-only={sorted(set(ready[0]) - set(listed[0]))}"
    )
