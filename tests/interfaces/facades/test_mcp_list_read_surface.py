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
import re
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


# ─────────────────────────── the response-size bound ────────────────────────────
# Bug daughterly-agitative-ocelot (494b-2dd3-e9d3-4fb0). Measured against the live
# deployed server on 2026-08-28, one unfiltered `tools/call list_tickets {}` returned
# 94,541,551 bytes over 177 seconds in a single JSON-RPC result. The server never
# errors; the CLIENT gives up, and how it gives up is client-specific -- GitHub Copilot
# CLI reports `Transport closed`, another client truncated to 219,815 chars and spilled
# to disk -- so no caller can tell "too big" from "server died". `_cap_workflow_payload`
# already states the contract (keep an MCP payload under the client's ~25K-token budget)
# but was wired to the two workflow reads only.


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
    client branches on a code instead of parsing prose. The exception TYPE is what matters:
    ``_envelope_error`` only envelopes a known rebar exception type, so a plain
    ``Exception`` carrying ``.error_code`` would be dropped and the caller would get an
    unstructured failure.
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


def test_ready_tickets_is_bounded_too(rebar_repo: Path, monkeypatch) -> None:
    """`ready_tickets` is the other whole-shape discovery surface; it gets the same bound.

    Its lean shape is covered above; this is the other half. On the real store its 61 ready
    tickets weighed 693,556 bytes unprojected and 76,878 lean, so the projection is what
    keeps an ordinary ``ready`` under the budget -- and the bound is what makes an
    extraordinary one fail loudly instead of killing the transport.
    """
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


def test_budget_is_measured_on_the_wire_payload_not_the_raw_reducer_rows() -> None:
    """The bound must not UNDER-estimate: measure what the client receives.

    Sizing the raw reducer dicts is wrong on two independent axes, and both make the
    measurement SMALLER than the truth -- which is the dangerous direction, because an
    under-estimating bound passes a payload that still overruns the client:

    1. ``TicketStateOut`` DECLARES defaults (`description`, `comments`, `deps`,
       `inbound_deps`, `file_impact`, `file_impact_scope`, `no_file_impact_reason`,
       `plan_review_health`, `cross_session_warning`), so ``model_validate`` RE-ADDS every
       field the lean projection just dropped, as an explicit ``null``/``[]``/``""``.
    2. ``js_safe_result`` rewrites each JS-unsafe 19-digit nanosecond integer as a QUOTED
       string, which is longer than the bare int.

    Measured at 2,855 rows (this store's size): raw dicts 552,772 bytes; after
    ``model_validate`` 1,526,327; after ``js_safe_result`` 1,537,747 -- the raw measurement
    under-reports the real payload by 178%, so a 90,000-byte budget measured that way would
    in truth pass payloads approaching 250,000 bytes.
    """
    from rebar._mcp_budget import _payload_bytes, _wire_bytes
    from rebar._mcp_errors import js_safe_result
    from rebar._mcp_models import TicketStateOut

    rows = []
    for index in range(20):
        state = _bulky_state(index)
        # Nanosecond timestamps as the reducer emits them: bare 19-digit ints.
        state["created_at"] = 1787858559072112001 + index
        state["updated_at"] = 1787949089260113711 + index
        rows.append({k: v for k, v in state.items() if k not in SIGNATURE_BULK})

    raw_measure = _payload_bytes({"result": rows})
    models = [TicketStateOut.model_validate(row) for row in rows]
    wire_measure = _wire_bytes(models)
    actual = len(json.dumps({"result": js_safe_result(models)}, default=str))

    assert wire_measure == actual, (
        f"the budget measured {wire_measure} bytes but the client receives {actual}"
    )
    assert raw_measure < actual, (
        "this test is vacuous unless the raw-dict measurement really is smaller "
        f"(raw={raw_measure} actual={actual})"
    )


def test_a_list_the_raw_measurement_would_have_passed_is_refused(
    rebar_repo: Path, monkeypatch
) -> None:
    """The regression window: under budget by the raw measurement, over it on the wire.

    250 plain rows (no signature bulk at all -- just the 19-digit `created_at`/`updated_at`
    every ticket carries) measure 49,652 bytes as raw reducer dicts, comfortably inside the
    90,000-byte budget, but reach the client as 135,902 bytes. A bound measured on the raw
    dicts RETURNS this list; a payload 51% over the budget it was supposed to enforce is
    exactly the "passes something that still overruns" failure the bound exists to prevent.
    """
    import rebar._engine_support.reads as reads_mod
    from rebar._mcp_budget import _LIST_TOKEN_BUDGET_BYTES, _payload_bytes

    def _plain(index: int) -> dict:
        return {
            "ticket_id": f"0000-0000-0000-{index:04x}",
            "ticket_type": "task",
            "title": f"ticket {index}",
            "status": "open",
            "priority": 2,
            "tags": [],
            "created_at": 1787858559072112001 + index,
            "updated_at": 1787949089260113711 + index,
        }

    rows = [_plain(index) for index in range(250)]
    assert _payload_bytes({"result": rows}) <= _LIST_TOKEN_BUDGET_BYTES, (
        "this test is vacuous unless the RAW measurement really is under budget"
    )
    monkeypatch.setattr(reads_mod, "reduce_all_tickets", lambda *a, **k: list(rows))

    response = _call("list_tickets")

    assert response.isError, (
        "a list that only LOOKS under budget as raw dicts must still be refused -- "
        "the client receives 135,902 bytes for it"
    )
    text = "".join(getattr(b, "text", "") for b in (response.content or []))
    message = json.loads(text[text.index("{") :])["message"]
    reported = int(re.search(r"would return (\d+) bytes", message).group(1))
    assert reported > _LIST_TOKEN_BUDGET_BYTES, (
        f"the refusal must quote the size it actually compared against: {message}"
    )


def _refusal_message(response) -> str:
    text = "".join(getattr(b, "text", "") for b in (response.content or []))
    return json.loads(text[text.index("{") :])["message"]


def test_the_refusal_names_a_remedy_the_TOOL_can_actually_execute(
    rebar_repo: Path, bulky_store, monkeypatch
) -> None:
    """A remedy the tool would reject is worse than no remedy: it is a dead end.

    ``ready_tickets`` takes only ``sort`` and ``full`` -- it has NO filter parameters, and
    ``full`` switches SHAPE, not which tickets come back -- so telling its caller to "narrow
    the query with status/has_tag" names arguments the tool would reject. ``ready`` is a
    whole-store question; the scoped tool is ``next_batch``.
    """
    bulky_store(4000)
    list_message = _refusal_message(_call("list_tickets"))
    for filter_name in ("status", "ticket_type", "has_tag", "without_tag"):
        assert filter_name in list_message, f"list_tickets can filter on {filter_name}"

    monkeypatch.setattr(rebar, "ready", lambda **_: [_bulky_state(i) for i in range(4000)])
    ready_message = _refusal_message(_call("ready_tickets"))
    assert "next_batch" in ready_message, (
        f"ready_tickets cannot be narrowed in place; its refusal must point at the "
        f"scoped tool that can: {ready_message}"
    )
    for absent in ("has_tag", "without_tag", "parent"):
        assert absent not in ready_message, (
            f"ready_tickets has no {absent} parameter -- naming it sends the caller into "
            f"a dead end: {ready_message}"
        )
