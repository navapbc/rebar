"""Register the MCP server's always-available read tools.

Split from ``build_server`` to keep its composition root small. The library-backed reads are
DERIVED from the annotated ``rebar.*`` signatures (``rebar._mcp_derive`` plus the override
table ``rebar._mcp_tool_specs``, ADR 0119); only the eight MCP-only reads are written here.
Tools return plain payloads; ``rebar._mcp_schema`` advertises each one's canonical JSON
Schema as its ``outputSchema``.
"""

from __future__ import annotations

from typing import Any

import rebar
from rebar._mcp_derive import context_gate, derive_tools
from rebar._mcp_schema import tool_annotation_presets


def register_bridge_tools(mcp, ctx) -> None:
    """Register the additive bridge reads and, on a writable server, the bridge mutations."""
    derive_tools(mcp, ctx, "bridge_read")
    if not context_gate(ctx, "readonly"):
        derive_tools(mcp, ctx, "bridge_mutation")


def _register_gate_status_reads(mcp, ann) -> None:
    """Register the read-only plan-review and completion currency polls."""

    @mcp.tool(annotations=ann["READ_ONLY"])
    def plan_review_status(ticket_id: str) -> dict[str, Any]:
        """Report plan-certificate currency using the exact claim-gate check.

        This read uses no LLM or network. It returns ``{ok, verdict, reason,
        verified_at_sha, signed_at}``; current is ``certified``, otherwise
        ``stale-code``, ``stale-head``, ``stale-material``, ``stale-reopened``,
        ``stale-pin-drift``, ``stale-pin-missing``, ``stale-store``, ``unsigned``,
        ``wrong-kind``, ``not-closed``, ``malformed-pin``, ``malformed-phase``,
        ``incompatible-phase``, ``unverifiable-material``, or ``error``. ``reason``
        names the drift; the SHA and timestamp are null without a readable certificate.
        """
        import rebar.llm

        return dict(rebar.llm.plan_review_status(ticket_id))

    @mcp.tool(annotations=ann["READ_ONLY"])
    def verify_completion_status(ticket_id: str) -> dict[str, Any]:
        """Report durable completion-certificate currency without an LLM or network.

        This local close-gate check lets callers poll completion runs without another
        charge. It returns ``{ok, verdict, reason, verified_at_sha, signed_at}``;
        ``verdict`` is ``certified`` or ``unsigned``; its SHA and timestamp are
        null when no certificate exists.
        """
        import rebar.llm

        return rebar.llm.verify_completion_status(ticket_id)

    @mcp.tool(annotations=ann["READ_ONLY"])
    def gate_status(job_id: str) -> dict[str, Any]:
        """Poll an async plan-review or completion run without executing it.

        Replaying the local gate index returns ``{job_id, status, ticket_id,
        gate_type, verdict?, error?, durable?, findings?}``. Status is ``running``,
        then ``passed`` or ``failed``; stale runs fail diagnostically, ``attaching``
        awaits a duplicate's index record, and ``unknown`` marks an unrecognized ID.
        ``durable`` reports signed-certificate currency. Read plan findings only after
        ``findings.readable`` is true.
        """
        import rebar.llm

        return dict(rebar.llm.gate_run_status(job_id))


def register_read_tools(mcp, ctx) -> None:
    """Register the always-available read tools on ``mcp`` (see module docstring)."""
    _cap_workflow_payload = ctx.cap_workflow_payload
    ann = tool_annotation_presets()

    derive_tools(mcp, ctx, "read")
    register_bridge_tools(mcp, ctx)
    _register_gate_status_reads(mcp, ann)

    @mcp.tool(annotations=ann["READ_ONLY"])
    def audit_trail(ticket_id: str) -> dict:
        """Read a ticket's complete plan, completion, and code-review audit trail.

        Plan and related code-review sidecar histories are newest-first; completion
        includes its attestation and sidecar. Individual sidecar failures degrade to
        ``[]`` or ``None``. This read remains available under ``REBAR_MCP_READONLY=1``.
        """
        from rebar.audit.read import audit_trail as _audit_trail

        return _audit_trail(ticket_id)

    @mcp.tool(annotations=ann["READ_ONLY"])
    def explain_criterion(criterion_id: str) -> dict:
        """Read a plan criterion or an author guide without an LLM.

        Guide names are ``plan`` (passing plans), ``review`` (code review), and
        ``commit-trailer`` (the required ``rebar-ticket:`` format), using the same
        lookup as ``rebar explain``. Failures return ``{error, kind, message}``, where
        kind is ``unknown-id``, ``malformed-registry``, or ``missing-file``.
        """
        from rebar.llm.plan_review import registry

        try:
            if criterion_id in registry.AUTHOR_GUIDES:
                guide = registry.explain_guide(criterion_id)
                return {"criterion_id": criterion_id, "section": guide}
            section = registry.explain_criterion(criterion_id)
            return {"criterion_id": criterion_id, "section": section}
        except registry.ExplainError as exc:
            # Map exc.kind to vocabulary code (ticket 8a31)
            kind_to_code = {
                "unknown-id": "criterion_unknown_id",
                "malformed-registry": "criterion_registry_malformed",
                "missing-file": "criterion_missing_file",
            }
            code = kind_to_code.get(exc.kind, "command_failed")
            return {"error": code, "kind": exc.kind, "message": str(exc)}

    @mcp.tool(annotations=ann["READ_ONLY"])
    def get_workflow_status(run_id: str, ticket_id: str | None = None) -> dict[str, Any]:
        """Read a workflow run's current status via replay (no execution) ->
        {run_id, ticket_id, workflow_name, status, terminal_step, error, steps}.

        Typed read tool (mirrors src/rebar/schemas/workflow_run.schema.json), always
        available. ``ticket_id`` is resolved from the local run index when omitted."""
        return _cap_workflow_payload(dict(rebar.get_workflow_status(run_id, ticket_id)))

    @mcp.tool(annotations=ann["READ_ONLY"])
    def get_workflow_result(run_id: str, ticket_id: str | None = None) -> dict[str, Any]:
        """Read a workflow run's outputs via replay -> {run_id, status,
        terminal_step, terminal_output, outputs, error}. The terminal step's output
        is the run result.

        Typed read tool (workflow_run schema), always available. Bulky outputs are
        elided to stay under the MCP token budget (``truncated: true``); read the
        full result via the library/CLI."""
        return _cap_workflow_payload(dict(rebar.get_workflow_result(run_id, ticket_id)))

    @mcp.tool(annotations=ann["READ_ONLY"])
    def render_workflow(workflow: str) -> str:
        """Render a workflow (a .rebar/workflows/<name> name or a file path) to a
        read-only Mermaid flowchart (TEXT; the host renders it to SVG, never
        committed). Large graphs degrade to a text outline. Read tool, always
        available."""
        from rebar.llm.workflow import render

        return render.render_workflow(workflow)
