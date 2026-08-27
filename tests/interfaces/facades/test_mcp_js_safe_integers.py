"""An MCP tool result must not put a JSON-uninteroperable integer on the wire
(bug unreal-milky-sloth / 6fe7-956f-4901-45cf).

RFC 8259 section 6 guarantees that implementations "agree exactly on their numeric
values" only for integers in ``[-(2**53)+1, (2**53)-1]``. rebar's ticket timestamps are
``time.time_ns()`` values -- 19 digits, far outside that range -- and they reached the MCP
wire as bare JSON numbers through ``_Out.model_config = ConfigDict(extra="allow")``
(``src/rebar/_mcp_models.py:31-36``), which lets ``created_at``/``updated_at`` pass through
``TicketStateOut`` undeclared.

Every supported MCP client parses JSON numbers as IEEE-754 binary64, so this broke both
ways: a client using plain ``JSON.parse`` SILENTLY truncated the value
(``1786649271159032001`` -> ``1786649271159032000``), and GitHub Copilot CLI, which parses
losslessly into a ``BigInt``, threw ``TypeError: Do not know how to serialize a BigInt``
when it re-stringified the result. rebar had already reached this conclusion for jq and
banned it from the event path (``src/rebar/_store/canonical.py:15-19``,
``tests/unit/test_canonical.py:64-72``); the MCP surface never generalized the rule.

These tests drive the real in-process MCP server -- both the direct ``call_tool`` path and
the REGISTERED request handler an actual stdio client reaches -- against a real store.
Nothing is mocked.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

import rebar

pytestmark = pytest.mark.unit

#: RFC 8259 section 6 interoperable integer range; also JS ``Number.MAX_SAFE_INTEGER``.
JS_SAFE_MAX = 2**53 - 1


def _unsafe_ints(node: Any, path: str = "$") -> list[tuple[str, int]]:
    """Every integer in ``node`` that JSON cannot carry interoperably."""
    if isinstance(node, bool):
        return []
    if isinstance(node, int):
        return [(path, node)] if abs(node) > JS_SAFE_MAX else []
    if isinstance(node, dict):
        return [hit for k, v in node.items() for hit in _unsafe_ints(v, f"{path}.{k}")]
    if isinstance(node, list):
        return [hit for i, v in enumerate(node) for hit in _unsafe_ints(v, f"{path}[{i}]")]
    return []


def _call_direct(tool: str, **args: object) -> tuple[str, Any]:
    """Call ``tool`` on the real server; return its (content text, structured) pair."""
    from rebar.mcp_server import build_server

    result = asyncio.run(build_server().call_tool(tool, args))
    if isinstance(result, tuple):
        blocks, structured = result
        return "".join(b.text for b in blocks), structured
    return "".join(b.text for b in result), None


def _call_via_handler(tool: str, **args: object) -> tuple[str, Any]:
    """Call ``tool`` through the REGISTERED request handler a stdio client reaches.

    ``FastMCP._setup_handlers`` binds the handler at construction time, so a fix applied
    only to the instance after ``build_server()`` would pass ``_call_direct`` and still
    ship broken. This path is the one an agent actually takes.
    """
    import mcp.types as types

    from rebar.mcp_server import build_server

    server = build_server()
    handler = server._mcp_server.request_handlers[types.CallToolRequest]
    request = types.CallToolRequest(
        method="tools/call", params=types.CallToolRequestParams(name=tool, arguments=dict(args))
    )
    response = asyncio.run(handler(request))
    payload = response.root
    text = "".join(getattr(b, "text", "") for b in (payload.content or []))
    return text, payload.structuredContent


@pytest.fixture
def ticket_with_ns_timestamp(rebar_repo: Path) -> str:
    """A real ticket whose stored ``created_at`` is a nanosecond int beyond the JS range."""
    ticket_id = rebar.create_ticket("task", "a ticket carrying a nanosecond timestamp")
    created_at = rebar.show_ticket(ticket_id)["created_at"]
    # Prove the precondition: without an out-of-range value there is nothing to assert on.
    assert isinstance(created_at, int), f"created_at is not an int: {created_at!r}"
    assert created_at > JS_SAFE_MAX, (
        f"fixture precondition failed: created_at {created_at} is inside the JS-safe range, "
        "so this test could not detect the defect"
    )
    return str(ticket_id)


@pytest.mark.parametrize("call", [_call_direct, _call_via_handler], ids=["direct", "handler"])
def test_show_ticket_emits_no_uninteroperable_integer(
    ticket_with_ns_timestamp: str, call: Any
) -> None:
    """The reported mechanism: a ticket record must not carry a bare >2**53 JSON number."""
    text, structured = call("show_ticket", ticket_id=ticket_with_ns_timestamp)

    structured_hits = _unsafe_ints(structured)
    assert not structured_hits, (
        "structuredContent carries integers outside the RFC 8259 interoperable range, "
        f"which a JS client turns into BigInt or silently truncates: {structured_hits}"
    )

    text_hits = _unsafe_ints(json.loads(text))
    assert not text_hits, f"the content text block carries uninteroperable integers: {text_hits}"


def test_list_tickets_emits_no_uninteroperable_integer(ticket_with_ns_timestamp: str) -> None:
    """The tool named in the report. ``list_tickets`` returns many ticket records.

    LIVENESS FIRST. ``assert not hits`` alone passes vacuously — on an empty list, on an
    error envelope, or on a payload carrying no timestamps at all — so a broken tool would
    read as green. The anchors below prove the payload actually contained the thing under
    test BEFORE the absence-of-unsafe-integers assertion is allowed to mean anything.
    """
    _text, structured = _call_via_handler("list_tickets", status="open")

    # Anchor 1: the call produced real ticket records, not an empty list or an error.
    rows = (structured or {}).get("result")
    assert isinstance(rows, list) and rows, (
        f"list_tickets returned no ticket rows, so this test would pass without checking "
        f"anything: {structured!r}"
    )

    # Anchor 2: the rows actually carry the fields this test exists to police.
    ours = [r for r in rows if r.get("ticket_id") == ticket_with_ns_timestamp]
    assert ours, f"the ticket under test is absent from the listing: {ticket_with_ns_timestamp}"
    row = ours[0]
    for field in ("created_at", "updated_at"):
        assert field in row, f"{field} missing from the ticket row; nothing to police: {row!r}"

    # Anchor 3: the value is genuinely outside the safe range, so the guard had work to do.
    assert int(row["created_at"]) > JS_SAFE_MAX, (
        f"created_at {row['created_at']!r} is inside the JS-safe range, so a broken guard "
        "would still pass this test"
    )

    # Only now is the absence assertion meaningful.
    hits = _unsafe_ints(structured)
    assert not hits, f"list_tickets emitted uninteroperable integers: {hits}"


def test_the_timestamp_survives_the_wire_without_losing_digits(
    ticket_with_ns_timestamp: str,
) -> None:
    """Making it JS-safe must not cost precision -- truncating would be the other bug.

    A client must be able to recover the EXACT stored nanosecond value, so the fix has to
    be a lossless representation change, not a rounded or scaled number.
    """
    stored = rebar.show_ticket(ticket_with_ns_timestamp)["created_at"]
    _text, structured = _call_via_handler("show_ticket", ticket_id=ticket_with_ns_timestamp)

    on_the_wire = structured["created_at"]
    assert int(on_the_wire) == stored, (
        f"the wire value {on_the_wire!r} does not recover the stored {stored}"
    )


# --- Step 7 sibling: output models that DECLARE a ns timestamp as ``int`` ---------------
#
# The guard transforms the tool's return value, but FastMCP builds structuredContent with
# ``output_model.model_validate(...)`` (mcp/server/fastmcp/utilities/func_metadata.py:129).
# Pydantic coerces a numeric string back to ``int`` for a field DECLARED ``int``, so such a
# field re-acquires a bare out-of-range JSON number in structuredContent while the content
# text block (built by ``pydantic_core.to_json`` on the transformed object) keeps the
# string. Reproduced on the real wire against a ticket carrying a plan-review attestation:
#     plan_review_status structuredContent signed_at: 1787770654610472000 (int)
#     plan_review_status content-text      signed_at: '1787770654610472000' (str)
# The two halves of one response disagreed, and the structured half still broke JS clients.

_NS_TIMESTAMP_FIELDS = ("signed_at", "created_at", "updated_at", "timestamp")


def _models_declaring_ns_timestamps() -> list[tuple[type, str]]:
    """Every MCP output model that declares one of the ns-timestamp field names."""
    from rebar import _mcp_models

    found = []
    for name in dir(_mcp_models):
        model = getattr(_mcp_models, name)
        fields = getattr(model, "model_fields", None)
        if not isinstance(fields, dict):
            continue
        for field in _NS_TIMESTAMP_FIELDS:
            if field in fields:
                found.append((model, field))
    return found


def test_a_declared_ns_timestamp_field_advertises_the_safe_string_form() -> None:
    """The advertised outputSchema must permit the decimal-string form.

    Asserts on ``model_json_schema()`` — the very bytes published to clients as a tool's
    ``outputSchema`` in ``tools/list`` — so this is client-observable output, not an
    internal name. A field advertised as ``integer`` only is also the field pydantic
    coerces the guard's string back into, which is what reintroduced the bare
    out-of-range number in structuredContent.
    """
    declared = _models_declaring_ns_timestamps()
    assert declared, "no MCP output model declares a ns-timestamp field — check the names"

    def _permits_string(node: dict) -> bool:
        declared_type = node.get("type")
        if declared_type == "string" or (
            isinstance(declared_type, list) and "string" in declared_type
        ):
            return True
        return any(_permits_string(sub) for sub in node.get("anyOf", []))

    offenders = [
        (model.__name__, field)
        for model, field in declared
        if not _permits_string(model.model_json_schema()["properties"][field])
    ]

    assert not offenders, (
        "these output models advertise a nanosecond timestamp as an integer only, so "
        "pydantic coerces the JS-safe decimal string back and structuredContent carries "
        f"an uninteroperable bare number again: {offenders}"
    )


# --- Advisory 2: the declared-int coercion path, tested ON THE WIRE ---------------------
#
# The structural guard above asserts the SCHEMA permits the string form. That is necessary
# but not sufficient: it would still pass if the guard stopped running, or if FastMCP
# changed how it builds structuredContent. This test exercises the actual layer where the
# first fix broke — `output_model.model_validate(...)` coercing a numeric string back to
# `int` for a field DECLARED `int` — through the registered request handler, and pins the
# extra symptom that made it insidious: the two halves of ONE response disagreed.
#     plan_review_status structuredContent signed_at: 1787770654610472000 (int)
#     plan_review_status content-text      signed_at: '1787770654610472000' (str)


@pytest.fixture
def signed_ticket(rebar_repo: Path) -> str:
    """A real ticket whose manifest has been signed, so ``signed_at`` is populated."""
    return str(rebar.create_ticket("task", "a ticket whose manifest gets signed"))


def test_declared_signed_at_reaches_the_wire_as_a_string(signed_ticket: str) -> None:
    """``signed_at`` is DECLARED ``int`` on its output models — the coercion trap."""
    text, structured = _call_via_handler(
        "sign_manifest", ticket_id=signed_ticket, manifest=["step one", "step two"]
    )

    # Liveness: prove the field is actually present before asserting anything about it.
    assert structured and "signed_at" in structured, (
        f"sign_manifest returned no signed_at, so this test would check nothing: {structured!r}"
    )
    signed_at = structured["signed_at"]
    assert int(signed_at) > JS_SAFE_MAX, (
        f"signed_at {signed_at!r} is inside the JS-safe range; this test cannot detect the bug"
    )

    assert isinstance(signed_at, str), (
        f"signed_at reached structuredContent as {type(signed_at).__name__} "
        f"({signed_at!r}) — model_validate coerced the JS-safe string back to a bare "
        "out-of-range integer, which is exactly what broke JS clients"
    )
    assert signed_at.isdigit(), f"signed_at is not bare decimal digits: {signed_at!r}"

    # The two response branches must agree — the divergence was the tell.
    assert json.loads(text)["signed_at"] == signed_at, (
        "the content text block and structuredContent disagree about signed_at"
    )
    assert not _unsafe_ints(structured), f"sign_manifest still emits bare big ints: {structured}"


def test_verify_signature_reads_the_same_signed_at_back_as_a_string(signed_ticket: str) -> None:
    """The read side of the same declared-``int`` field, and it must round-trip exactly."""
    _t, signed = _call_via_handler("sign_manifest", ticket_id=signed_ticket, manifest=["step one"])
    text, structured = _call_via_handler("verify_signature", ticket_id=signed_ticket)

    assert structured and structured.get("signed_at") is not None, (
        f"verify_signature returned no signed_at; nothing to check: {structured!r}"
    )
    assert isinstance(structured["signed_at"], str), (
        f"verify_signature emitted signed_at as {type(structured['signed_at']).__name__}"
    )
    assert int(structured["signed_at"]) == int(signed["signed_at"]), (
        "verify_signature did not read back the exact instant sign_manifest wrote — the "
        "string form must be lossless, not merely well-typed"
    )
    assert json.loads(text)["signed_at"] == structured["signed_at"]
    assert not _unsafe_ints(structured)


def test_plan_review_status_never_emits_a_bare_out_of_range_signed_at(signed_ticket: str) -> None:
    """The third declared-``int`` ``signed_at``.

    Weaker by necessity: populating it needs a certified plan-review attestation, which
    needs a live LLM call, so an unattested ticket reports ``signed_at: null``. The
    assertion that still holds unconditionally — and is the one that failed before the fix
    — is that the field is NEVER a bare integer outside the interoperable range.
    """
    _text, structured = _call_via_handler("plan_review_status", ticket_id=signed_ticket)

    assert structured and "signed_at" in structured, (
        f"plan_review_status returned no signed_at key: {structured!r}"
    )
    signed_at = structured["signed_at"]
    assert signed_at is None or isinstance(signed_at, str), (
        f"signed_at must be null or a decimal string, got {type(signed_at).__name__}: {signed_at!r}"
    )
    assert not _unsafe_ints(structured), f"plan_review_status emits bare big ints: {structured}"
