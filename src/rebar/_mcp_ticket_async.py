"""Async starter for the ticket operation that runs a gate inline (bug cb1b).

``transition_ticket`` runs the completion-verifier close gate inline on a close. That takes
minutes, far past an MCP client's ~60 s request deadline, so the synchronous tool times out
with an ambiguous ``-32001`` while the server keeps working, and the natural re-fire bills a
second gate pass. (``claim_ticket`` and the ``open -> in_progress`` transition only run the
local, no-LLM ``claim_gate_check``, so they return quickly and need no starter.)

``transition_ticket_start`` runs the SAME derived tool (the spec's pre/post hooks included) on
the gate daemon from :mod:`rebar._mcp_llm` and returns a
``{job_id, ticket_id, gate_type, status: "running"}`` handle at once. The status change is
applied only when the gate passes; ``gate_status(job_id)`` then reads ``passed`` with verdict
``applied``, or ``failed`` with the refusal in ``error``.

The starter owns the gate type ``transition`` rather than reusing ``verify_completion``. The
dedup key starts with the gate type, so reusing it would let a close ATTACH to a running
``verify_completion_start`` for the same ticket, report that check's ``passed``, and never
transition at all.

This is a write tool: the registrar is skipped on a read-only server, like
``register_write_tools``.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

from rebar._mcp_derive import make_tool
from rebar._mcp_llm import _start_gate_job
from rebar._mcp_schema import tool_annotation_presets

# The verdict a ticket-op job records once the library call returns: the transition landed.
APPLIED = "applied"

# (sync tool, gate type of its starter, starter description)
_STARTERS: tuple[tuple[str, str, str], ...] = (
    (
        "transition_ticket",
        "transition",
        """Start ``transition_ticket`` on a background daemon and immediately return
        ``{job_id, ticket_id, gate_type: "transition", status: "running"}``.

        Takes exactly ``transition_ticket``'s arguments. Prefer it for a close, whose
        completion-verifier gate takes minutes: the transition and its gate run on the
        daemon, and the status changes only if the gate passes. Poll
        ``gate_status(job_id)``: ``passed`` with verdict ``applied`` means the transition
        landed; ``failed`` carries the refusal in ``error``. A start with the same
        arguments while one runs attaches to it instead of running the gate twice.
        """,
    ),
)


def _variant(arguments: dict[str, Any]) -> str:
    """Dedup variant: every argument except the ticket, so only an identical call attaches."""
    return ";".join(f"{k}={arguments[k]!r}" for k in sorted(arguments) if k != "ticket_id")


def _applied(result: Any) -> dict[str, Any]:
    """Mark a completed library call as applied for the run index; keep its own fields."""
    out = dict(result) if isinstance(result, dict) else {"result": result}
    out["verdict"] = APPLIED
    return out


def _make_starter(sync: Callable[..., Any], name: str, gate_type: str, doc: str):
    """Build the async starter for the derived sync tool ``sync``, with its exact signature."""
    sig: inspect.Signature = sync.__signature__  # type: ignore[attr-defined]

    async def starter(*args: Any, **kwargs: Any) -> dict:
        bound = sig.bind(*args, **kwargs)
        bound.apply_defaults()
        arguments = dict(bound.arguments)
        # Basis resolution and the index write block, so keep them off the event loop.
        import anyio.to_thread  # deferred: keep this module importable with core deps only

        return await anyio.to_thread.run_sync(
            lambda: _start_gate_job(
                gate_type,
                arguments["ticket_id"],
                ref=arguments.get("ref"),
                # The close gate verifies the local HEAD by default, so key on it.
                source="local",
                variant=_variant(arguments),
                readonly=False,
                force=False,
                work=lambda: _applied(sync(**arguments)),
            )
        )

    starter_sig = sig.replace(return_annotation=dict)
    starter.__name__ = starter.__qualname__ = name
    starter.__doc__ = doc
    starter.__signature__ = starter_sig  # type: ignore[attr-defined]
    starter.__annotations__ = {
        **{p.name: p.annotation for p in starter_sig.parameters.values()},
        "return": dict,
    }
    return starter


def register_ticket_async_tools(mcp, ctx) -> None:
    """Register ``transition_ticket_start`` unless the server is read-only."""
    if ctx.readonly():
        return
    from rebar._mcp_tool_specs import TOOL_SPECS

    ann = tool_annotation_presets()
    specs = {spec.name: spec for spec in TOOL_SPECS}
    for tool_name, gate_type, doc in _STARTERS:
        spec = specs[tool_name]
        starter = _make_starter(make_tool(spec, ctx), f"{tool_name}_start", gate_type, doc)
        mcp.tool(annotations=ann[spec.annotation])(starter)
