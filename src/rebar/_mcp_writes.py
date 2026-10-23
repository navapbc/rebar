"""Register MCP write tools only when the server permits writes.

Library-backed tools derive from ``rebar.*`` signatures and spec overrides;
``run_workflow`` is the MCP-only async tool defined here. Read-only mode registers
no write tools, keeping them absent from ``list_tools()``.
"""

from __future__ import annotations

import rebar  # noqa: F401  (tool bodies resolve rebar.<name> per call; tests patch it here)
from rebar._mcp_derive import derive_tools
from rebar._mcp_schema import tool_annotation_presets
from rebar._opcert_binding import spawn_context_daemon


def register_write_tools(mcp, ctx) -> None:
    """Register write tools unless this server is read-only."""
    if ctx.readonly():
        return
    _allow_llm = ctx.allow_llm
    logger = ctx.logger
    ann = tool_annotation_presets()

    derive_tools(mcp, ctx, "write")

    @mcp.tool(annotations=ann["MUTATE_OPEN_WORLD"])
    async def run_workflow(
        workflow: str,
        ticket_id: str,
        inputs: dict | None = None,
        dry_run: bool = False,
        ref: str | None = None,
        source: str | None = None,
    ) -> dict:
        """Start a workflow run; returns {run_id, ticket_id, status:'running'}
        IMMEDIATELY (async — the run executes on a background **daemon thread**, so
        it survives client request timeouts). Poll get_workflow_status /
        get_workflow_result to read its outcome. DURABILITY IS LIMITED: the daemon
        thread does NOT survive the MCP process exiting, and there is NO reaper or
        automatic resume — if the process dies mid-run the run is left ``running``
        forever and nothing re-drives it. Step effects ARE persisted to
        ``ticket_id``'s event log with idempotency markers, so a run can be
        **resumed only by explicitly re-invoking it** (already-completed steps are
        then skipped); it does not resume on its own. ``workflow`` is a
        .rebar/workflows/<name> name or a file path; ``dry_run`` executes agent
        steps with the offline FakeRunner (no tokens). Write tool (gated by
        REBAR_MCP_READONLY).

        A workflow with LLM/agent steps reads a snapshot pinned at ``ref`` (default
        ``origin/main``) in ``source=attested`` (default) mode — never the server's
        mutable checkout — and is DISABLED unless REBAR_MCP_ALLOW_LLM=1 (it makes
        live, billable LLM calls), exactly like the other agentic tools. A
        deterministic-only workflow needs neither."""

        from rebar.llm.workflow import executor as _wf_exec
        from rebar.llm.workflow import runs as _wf_runs

        # A workflow that runs tool-using agents is a live, billable LLM op — fence it
        # behind the SAME gate as review_*/verify_* (dry_run is offline, so exempt).
        if not dry_run:
            try:
                _doc = _wf_runs.load_workflow_doc(workflow, None)
            except Exception:  # noqa: BLE001 — a load error surfaces in the run record below
                _doc = None
            if _doc is not None and _wf_runs.has_llm_steps(_doc) and not _allow_llm():
                raise ValueError(
                    f"run_workflow on {workflow!r} is disabled: it runs tool-using LLM "
                    "agent steps (a live, billable LLM call). Set REBAR_MCP_ALLOW_LLM=1 "
                    "to enable it, or pass dry_run=true for the offline runner."
                )

        run_id = _wf_exec.new_run_id()
        # Record the index AND an initial 'running' marker BEFORE returning, so an
        # immediate get_workflow_status poll resolves and sees the run (the
        # background thread overwrites the record with the full result, LWW).
        _wf_runs.record_run_location(run_id, ticket_id, None)
        _wf_exec.TicketEventRecorder(ticket_id).run_started(
            {"run_id": run_id, "status": "running", "workflow_name": workflow}
        )

        def _bg() -> None:
            # Record failures before the executor loop so polling cannot remain
            # stuck at ``running``; step failures persist through the executor.
            try:
                _wf_runs.run(
                    workflow,
                    inputs or {},
                    ticket_id=ticket_id,
                    run_id=run_id,
                    dry_run=dry_run,
                    ref=ref,
                    source_mode=source,
                )
            except Exception as exc:  # noqa: BLE001 — background run failure is reflected in run-state, not raised
                try:
                    _wf_exec.TicketEventRecorder(ticket_id).run_finished(
                        {"run_id": run_id, "status": "failed", "error": str(exc)}
                    )
                except Exception:
                    # Don't let a failure in the error-reporting path hide the
                    # original run failure: log BOTH (the recorder error with its
                    # traceback, and the original run error it was trying to record).
                    logger.warning(
                        "failed to record workflow run %s failure (original run error: %s)",
                        run_id,
                        exc,
                        exc_info=True,
                    )

        # Context-propagating spawn: a bare threading.Thread would drop the op-cert signer
        # binding and sign the bound principal under an unbound genesis key (bug ff4a).
        spawn_context_daemon(_bg, name=f"rebar-workflow-{run_id}")
        return {"run_id": run_id, "ticket_id": ticket_id, "status": "running"}
