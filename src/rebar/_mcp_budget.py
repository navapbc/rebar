"""MCP response budgets shared by workflow and list tool bodies.

Workflow bulk is safely re-readable, so oversized results are flagged and truncated.
Oversized lists are refused because a partial list is indistinguishable from a complete
answer. Both policies measure through the same payload-sizing seam.
"""

from __future__ import annotations

from typing import TypeVar

#: A list row: the validated MCP output model (``TicketStateOut``), kept generic so the
#: budget module stays free of a dependency on the model layer.
_Row = TypeVar("_Row")

# Keep MCP workflow status/result payloads under the client's ~25K-token budget
# (WS-ffc4). ~90 KB ≈ 25K tokens; over it, elide the bulky step outputs (which an
# agent can re-read via the library/CLI) while preserving the schema-valid shape.
_WORKFLOW_TOKEN_BUDGET_BYTES = 90_000


def _payload_bytes(payload: dict) -> int:
    import json

    return len(json.dumps(payload, default=str))


def _cap_workflow_payload(payload: dict) -> dict:
    """Bound a status/result payload under the ~25K-token MCP budget (WS-ffc4).

    Truncates the bulky carriers in escalating order until the WHOLE payload fits —
    bulk can live in `outputs`/`terminal_output` (result read) OR `steps` (status
    read) OR `error`/elsewhere — so the budget is airtight regardless of shape. The
    full result stays available via the library/CLI."""
    if _payload_bytes(payload) <= _WORKFLOW_TOKEN_BUDGET_BYTES:
        return payload
    note = (
        "[truncated to stay under the MCP token budget — read the full result via "
        "rebar.get_workflow_result / `rebar workflow result`]"
    )
    capped = dict(payload)
    capped["truncated"] = True
    # 1) elide the result carriers.
    if capped.get("terminal_output"):
        capped["terminal_output"] = {"_truncated": note}
    if isinstance(capped.get("outputs"), dict):
        capped["outputs"] = {sid: {"_truncated": note} for sid in capped["outputs"]}
    # 2) still over? collapse the per-step status map to a count (status read).
    if _payload_bytes(capped) > _WORKFLOW_TOKEN_BUDGET_BYTES and isinstance(
        capped.get("steps"), dict
    ):
        capped["steps"] = {"_truncated": f"{len(capped['steps'])} steps; {note}"}
    # 3) last resort: a minimal envelope that is guaranteed to fit + schema-valid.
    if _payload_bytes(capped) > _WORKFLOW_TOKEN_BUDGET_BYTES:
        capped = {
            "run_id": str(payload.get("run_id", "")),
            "status": str(payload.get("status", "")),
            "ticket_id": payload.get("ticket_id"),
            "workflow_name": payload.get("workflow_name"),
            "truncated": True,
            "error": note,
        }
    return capped


# Lists share the workflow's ~25K-token client budget. Refusal replaces ambiguous
# transport failure or silent truncation for very large whole-store reads.
_LIST_TOKEN_BUDGET_BYTES = _WORKFLOW_TOKEN_BUDGET_BYTES

#: Give each refusal an executable remedy. ``ready_tickets`` cannot filter, so its
#: remedy redirects to conflict-aware ``next_batch`` rather than naming invalid args.
_LIST_REMEDIES: dict[str, str] = {
    "list_tickets": (
        "Narrow the query with one of status, ticket_type, priority, parent, has_tag, "
        "without_tag -- or read a single ticket with show_ticket."
    ),
    "search": (
        "Narrow the search: make the query more specific, add a field predicate "
        "(status:, type:, priority:, assignee:, tag:, parent:), or pass one of the "
        "status, ticket_type, has_tag arguments -- or read a single ticket with show_ticket."
    ),
    "ready_tickets": (
        "ready_tickets takes no filter arguments, so this list cannot be narrowed in "
        "place: call next_batch(epic_id) for a scoped, conflict-aware batch of the same "
        "unblocked work, or use list_tickets, which does accept filters -- or read a "
        "single ticket with show_ticket."
    ),
}

#: Fallback for a tool with no bespoke entry: the filter list, which is right for every
#: current list surface except ``ready_tickets``.
_DEFAULT_LIST_REMEDY = _LIST_REMEDIES["list_tickets"]


def _wire_bytes(rows: list[_Row]) -> int:
    """Measure the validated list payload exactly as the client receives it.

    Model defaults and quoted JS-unsafe integers enlarge reducer rows, while FastMCP
    emits both structured content and an indented text block per row. Callers therefore
    pass post-validation rows; this sizes both post-``js_safe_result`` wire halves.
    """
    import json

    from rebar._mcp_errors import js_safe_result

    safe = js_safe_result(list(rows))
    structured = _payload_bytes({"result": safe})
    # One content text block per row, indent=2 -- FastMCP's own serialization.
    content = sum(len(json.dumps(row, default=str, indent=2)) for row in safe)
    return structured + content


def _bound_list_payload(rows: list[_Row], *, tool: str) -> list[_Row]:
    """Return ``rows`` unchanged, or REFUSE with a structured error when over budget.

    ``rows`` are the VALIDATED output models, not the raw reducer dicts -- see
    :func:`_wire_bytes` for why sizing the raw dicts under-estimates the real payload.

    Deliberately NOT the truncate-and-flag treatment `_cap_workflow_payload` gives a
    workflow payload: a shortened list is indistinguishable from a complete one, so
    truncating here would hand the caller a confidently wrong answer. The refusal is
    raised as a `RebarError` carrying `error_code="response_too_large"`, which the
    already-installed error guard (`rebar._mcp_errors.install_error_guard`) converts into
    the same structured envelope every other rebar MCP failure uses -- so a client
    branches on a code instead of parsing prose, and never sees a bare transport close.
    """
    size = _wire_bytes(rows)
    if size <= _LIST_TOKEN_BUDGET_BYTES:
        return rows
    from rebar._errors import RebarError

    err = RebarError(
        f"{tool} matched {len(rows)} tickets and would return {size} bytes, over the "
        f"{_LIST_TOKEN_BUDGET_BYTES}-byte MCP response budget. No partial list is returned "
        "(a short list cannot be told from a complete one). "
        + _LIST_REMEDIES.get(tool, _DEFAULT_LIST_REMEDY)
    )
    err.error_code = "response_too_large"  # type: ignore[attr-defined]
    raise err
