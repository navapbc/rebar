"""A large `list_tickets` must be BOUNDED and must fail LOUDLY (bug
daughterly-agitative-ocelot / 494b-2dd3-e9d3-4fb0).

Measured against the live deployed server on 2026-08-28, one unfiltered
``tools/call list_tickets {}`` returned **94,541,551 bytes over 177 seconds** in a single
JSON-RPC result. The server never errors; the CLIENT gives up, and how it gives up is
client-specific -- GitHub Copilot CLI reports ``Transport closed``, another client
truncated to 219,815 chars and spilled to disk -- so no caller can tell "too big" from
"server died".

Two defects produce that, and both are covered here:

1. **No bound.** ``_cap_workflow_payload`` (``src/rebar/mcp_server.py``) states the
   contract -- keep an MCP payload under the client's ~25K-token budget -- but it is wired
   to ``get_workflow_status``/``get_workflow_result`` ONLY. ``list_tickets`` had no size
   check at all. A list cannot be silently truncated the way a workflow payload can (a
   short list is indistinguishable from a complete one), so an over-budget list must RAISE
   a structured, actionable error.
2. **"Lean" was not lean.** ``list_tickets``' docstring promises a lean default that omits
   the bulky fields, but the lean path dropped only ``description``/``comments``. On the
   real store that left ``authorship_ledger`` (26.2 MB, 59%), ``attestations`` (10.4 MB)
   and ``signature`` (2.6 MB) in every row -- 88% of the "lean" payload was base64
   signature material no list caller consumes. Per row: 15,548 bytes vs 1,726 bytes.
   Without this, the bound in (1) would reject even ``list_tickets(status="open")``
   (51 rows, 224,610 bytes lean / 58,867 bytes slim).
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
#: removes them from the wire without touching the output schema.
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
def bulky_store(monkeypatch: pytest.MonkeyPatch, count: int = 3):
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


def test_oversize_list_is_refused_with_a_structured_error_not_a_short_list(
    rebar_repo: Path, bulky_store
) -> None:
    """Over budget -> a structured, actionable error. NEVER a truncated list.

    A silently-shortened list is the vacuous result this project refuses: the caller
    cannot tell it from a complete one. Asserted on the REGISTERED handler a real client
    reaches, so a guard applied only to the instance after ``build_server()`` cannot pass
    this and still ship broken.
    """
    from rebar.mcp_server import _LIST_TOKEN_BUDGET_BYTES

    bulky_store(4000)
    response = _call("list_tickets")

    assert response.isError, "an over-budget list must not come back as a result"
    text = "".join(getattr(b, "text", "") for b in (response.content or []))
    envelope = json.loads(text[text.index("{") :])
    assert envelope["error"] == "response_too_large", envelope
    assert "response_too_large" in rebar.KNOWN_ERROR_CODES
    message = envelope["message"]
    assert "4000" in message, f"error must name the matching row count: {message}"
    assert str(_LIST_TOKEN_BUDGET_BYTES) in message, f"error must name the budget: {message}"
    for hint in ("status", "ticket_type", "has_tag"):
        assert hint in message, f"error must name a narrowing filter ({hint}): {message}"
    # And no partial payload rode along with the refusal.
    assert response.structuredContent is None, response.structuredContent


def test_oversize_list_travels_the_established_envelope_seam(rebar_repo: Path, bulky_store) -> None:
    """The refusal must reuse `install_error_guard`, not a bespoke error path.

    On the direct ``call_tool`` surface that means ``ToolError`` raised *from* an
    ``McpEnvelopeError`` — the same delivery every other rebar MCP failure uses, so a
    client branches on a code instead of parsing prose.
    """
    import asyncio

    from mcp.server.fastmcp.exceptions import ToolError

    from rebar._mcp_errors import McpEnvelopeError
    from rebar.mcp_server import build_server

    bulky_store(4000)
    with pytest.raises(ToolError) as excinfo:
        asyncio.run(build_server().call_tool("list_tickets", {}))
    cause = excinfo.value.__cause__
    assert isinstance(cause, McpEnvelopeError), f"not the envelope seam: {cause!r}"
    assert cause.envelope["error"] == "response_too_large"


def test_ready_tickets_is_lean_and_bounded_too(rebar_repo: Path, monkeypatch) -> None:
    """`ready_tickets` is the other whole-shape discovery surface; same two defects.

    On the real store its 61 ready tickets weighed 693,556 bytes unprojected and 76,878
    bytes lean, so without the projection the bound alone would refuse an ordinary
    ``ready``.
    """
    rows = [_bulky_state(i) for i in range(3)]
    monkeypatch.setattr(rebar, "ready", lambda **_: list(rows))
    lean = _rows(_call("ready_tickets").structuredContent)
    assert not {f for row in lean for f in SIGNATURE_BULK if row.get(f)}
    full = _rows(_call("ready_tickets", full=True).structuredContent)
    assert all(row.get("authorship_ledger") for row in full)

    monkeypatch.setattr(rebar, "ready", lambda **_: [_bulky_state(i) for i in range(4000)])
    response = _call("ready_tickets")
    assert response.isError
    text = "".join(getattr(b, "text", "") for b in (response.content or []))
    assert json.loads(text[text.index("{") :])["error"] == "response_too_large"


def test_a_list_that_fits_is_returned_whole(rebar_repo: Path, bulky_store) -> None:
    """The bound must not cost the ordinary case: an under-budget list is unchanged."""
    bulky_store(5)
    rows = _rows(_call("list_tickets").structuredContent)
    assert len(rows) == 5
