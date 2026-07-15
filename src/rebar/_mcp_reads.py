"""Read-tool registrar for the rebar MCP server.

``register_read_tools(mcp, ctx)`` registers the always-available read tools on a
FastMCP server. Split out of ``rebar.mcp_server.build_server`` (which was a single
~700-LOC function) as a pure structural refactor — the tool names, signatures,
docstrings, and outputSchemas are behaviour-identical to their in-line originals.

The tools capture shared handles off ``ctx`` (a ``SimpleNamespace`` built in
``build_server``): the ``_readonly`` / ``_allow_jira_sync`` gate helpers, the
``MODE_CAPS`` / ``Mode`` reconcile tables, and the ``_cap_workflow_payload`` budget
helper. They are rebound to their original local names below so the tool bodies are
copied verbatim. Output models are imported at module level (FastMCP resolves a
tool's return annotation against THIS module's globals).
"""

from __future__ import annotations

import rebar
from rebar._mcp_models import (
    BridgeFsckOut,
    ClarityResultOut,
    DepsGraphOut,
    FileImpactItemOut,
    GateResultOut,
    GroundingInfoOut,
    NextBatchOut,
    TicketStateOut,
    ValidateReportOut,
    VerifyCommandItemOut,
    VerifySignatureResultOut,
    WorkflowRunOut,
    tool_annotation_presets,
)


def register_read_tools(mcp, ctx) -> None:
    """Register the always-available read tools on ``mcp`` (see module docstring)."""
    _readonly = ctx.readonly
    _allow_jira_sync = ctx.allow_jira_sync
    _cap_workflow_payload = ctx.cap_workflow_payload
    MODE_CAPS = ctx.MODE_CAPS
    Mode = ctx.Mode

    # ── Read tools ────────────────────────────────────────────────────────────
    _ANN = tool_annotation_presets()

    @mcp.tool(annotations=_ANN["READ_ONLY"])
    def show_ticket(ticket_id: str) -> TicketStateOut:
        """Show compiled ticket state (accepts full id, short id, or alias)."""
        return TicketStateOut.model_validate(rebar.show_ticket(ticket_id))

    @mcp.tool(annotations=_ANN["READ_ONLY"])
    def explain_criterion(criterion_id: str) -> dict:
        """Explain a plan-review criterion — its authoring-guide section (epic cite-stone-sea /
        WS10). A pure registry/guide READ (no LLM, so it is NOT gated on REBAR_MCP_ALLOW_LLM); the
        SAME shared lookup as the `rebar explain` CLI. On failure returns a structured error
        ``{error, kind}`` (kind ∈ unknown-id / malformed-registry / missing-file)."""
        from rebar.llm.plan_review import registry

        try:
            section = registry.explain_criterion(criterion_id)
            return {"criterion_id": criterion_id, "section": section}
        except registry.ExplainError as exc:
            return {"error": str(exc), "kind": exc.kind}

    @mcp.tool(annotations=_ANN["READ_ONLY"])
    def list_tickets(
        status: str | None = None,
        ticket_type: str | None = None,
        priority: int | None = None,
        parent: str | None = None,
        has_tag: str | None = None,
        without_tag: str | None = None,
        include_archived: bool = False,
        exclude_deleted: bool = False,
        min_children: int | None = None,
        blocking_state: str = "",
        with_children_count: bool = False,
        sort: str | None = None,
        full: bool = False,
    ) -> list[TicketStateOut]:
        """List tickets as a JSON array, with optional filters.

        ``exclude_deleted`` drops tickets whose reduced status is ``deleted``.
        delete writes STATUS(deleted)+ARCHIVED, so the default list already hides
        tombstones via archived-exclusion; ``exclude_deleted`` only changes
        results when combined with ``include_archived=True``. Each item carries a
        ``children_count``; ``min_children`` keeps tickets with >= N direct
        children, and ``blocking_state`` ("unblocked"/"blocked") filters by
        readiness (all blockers closed vs an open blocker).

        The list is **lean by default** — the bulky ``description`` and
        ``comments`` fields are omitted so a broad list stays small. Pass
        ``full=True`` for the complete ticket shape (or use ``show_ticket`` for a
        single ticket's body).
        """
        return [
            TicketStateOut.model_validate(t)
            for t in rebar.list_tickets(
                status=status,
                ticket_type=ticket_type,
                priority=priority,
                parent=parent,
                has_tag=has_tag,
                without_tag=without_tag,
                include_archived=include_archived,
                exclude_deleted=exclude_deleted,
                min_children=min_children,
                blocking_state=blocking_state,
                with_children_count=with_children_count,
                sort=sort,
                full=full,
            )
        ]

    @mcp.tool(annotations=_ANN["READ_ONLY"])
    def ticket_deps(ticket_id: str) -> DepsGraphOut:
        """Show the dependency graph for a ticket."""
        return DepsGraphOut.model_validate(rebar.deps(ticket_id))

    @mcp.tool(annotations=_ANN["READ_ONLY"])
    def audit_trail(ticket_id: str) -> dict:
        """The full audit read surface for a ticket (story 46f0): its FULL retained
        plan-review sidecar history (newest-first), its completion attestation + sidecar
        record, and the associated code reviews (``code_review`` tickets that link
        ``relates_to`` this ticket, each with its own retained sidecar history). Best-effort
        aggregation over the observability sidecars — individual reader failures degrade to
        ``[]`` / ``None`` rather than raising. Always available (a read tool, so it is served
        even under ``REBAR_MCP_READONLY=1``)."""
        from rebar.audit.read import audit_trail as _audit_trail

        return _audit_trail(ticket_id)

    @mcp.tool(annotations=_ANN["READ_ONLY"])
    def ready_tickets(sort: str | None = None) -> list[TicketStateOut]:
        """List tickets ready to work (all blockers closed). ``sort`` orders by
        ``priority|created|updated|id|status`` (prefix ``-`` for descending;
        unset values sort last)."""
        return [TicketStateOut.model_validate(t) for t in rebar.ready(sort=sort)]

    @mcp.tool(annotations=_ANN["READ_ONLY"])
    def next_batch(epic_id: str) -> NextBatchOut:
        """Next parallel batch of unblocked tickets under an epic's hierarchy."""
        return NextBatchOut.model_validate(rebar.next_batch(epic_id))

    @mcp.tool(annotations=_ANN["READ_ONLY"])
    def search(
        query: str,
        status: str | None = None,
        ticket_type: str | None = None,
        has_tag: str | None = None,
        include_archived: bool = False,
        sort: str | None = None,
    ) -> list[TicketStateOut]:
        """Full-text search over titles/descriptions/comments/tags (replay-derived).

        ``query`` accepts field predicates — ``status:``/``type:``/``priority:``/
        ``assignee:``/``tag:``/``parent:`` (comma = OR within a field; ``priority``
        accepts ``<``/``<=``/``>``/``>=`` and ``n..m`` ranges) and ``-``/``not:``
        negation; an unknown ``field:`` degrades to a literal substring. ``sort``
        orders by ``priority|created|updated|id|status`` (``-`` prefix = descending;
        unset values last)."""
        return [
            TicketStateOut.model_validate(t)
            for t in rebar.search(
                query,
                status=status,
                ticket_type=ticket_type,
                has_tag=has_tag,
                include_archived=include_archived,
                sort=sort,
            )
        ]

    @mcp.tool(annotations=_ANN["READ_ONLY"])
    def recent_session_logs(limit: int = 5) -> list[TicketStateOut]:
        """The newest session_log tickets, newest first (by created_at; default
        limit 5). session_logs are hidden from list_tickets; this is the
        type-specific read that surfaces them."""
        return [TicketStateOut.model_validate(t) for t in rebar.recent_session_logs(limit=limit)]

    @mcp.tool(annotations=_ANN["MUTATE_IDEMPOTENT"])
    def fsck(recover: bool = False) -> str:
        """Check ticket-store integrity (JSON validity, CREATE presence, lock
        cleanup). Set recover=True to run the recovery path."""
        if recover and _readonly():
            raise ValueError(
                "fsck recover=True is a write operation and is disabled: this "
                "server is read-only (REBAR_MCP_READONLY)"
            )
        # Plain fsck still mutates: it removes a stale .git/index.lock. On a
        # read-only server suppress that write (report the stale lock instead).
        return rebar.fsck(recover=recover, report_only=_readonly())

    # ── Quality gates + file-impact reads (WS5d) ───────────────────────────────
    @mcp.tool(annotations=_ANN["READ_ONLY"])
    def clarity_check(ticket_id: str) -> ClarityResultOut:
        """Score ticket clarity (score / verdict / threshold / passed)."""
        return ClarityResultOut.model_validate(rebar.clarity_check(ticket_id))

    @mcp.tool(annotations=_ANN["READ_ONLY"])
    def check_ac(ticket_id: str) -> GateResultOut:
        """Check the ticket has an Acceptance Criteria block
        ({verdict, criteria_count, reason, passed})."""
        return GateResultOut.model_validate(rebar.check_ac(ticket_id))

    @mcp.tool(annotations=_ANN["READ_ONLY"])
    def quality_check(ticket_id: str) -> GateResultOut:
        """Check ticket dispatch readiness ({verdict, line_count, keyword_count,
        ac_items, file_impact, reason, passed})."""
        return GateResultOut.model_validate(rebar.quality_check(ticket_id))

    @mcp.tool(annotations=_ANN["READ_ONLY"])
    def validate() -> ValidateReportOut:
        """Repo-wide quality health check (JSON report: score, critical/major/
        minor issues, warnings, suggestions). Takes no ticket id."""
        return ValidateReportOut.model_validate(rebar.validate())

    @mcp.tool(annotations=_ANN["READ_ONLY"])
    def get_file_impact(ticket_id: str) -> list[FileImpactItemOut]:
        """Get the file-impact array (consumed by next-batch conflict scheduling)."""
        return [FileImpactItemOut.model_validate(e) for e in rebar.get_file_impact(ticket_id)]

    @mcp.tool(annotations=_ANN["READ_ONLY"])
    def get_verify_commands(ticket_id: str) -> list[VerifyCommandItemOut]:
        """Get the DD-level verify-commands array for a ticket."""
        return [
            VerifyCommandItemOut.model_validate(e) for e in rebar.get_verify_commands(ticket_id)
        ]

    @mcp.tool(annotations=_ANN["READ_ONLY"])
    def grounding_info() -> GroundingInfoOut:
        """The STATIC code-grounding oracle integration contract (epic 8f6c): the
        closed dimension-ID vocabulary + version, the reference kinds, the closed
        abstain-reason enum (+ outcome/job/tier vocabularies), and the available
        backends with their detected availability/version. A fast, deterministic,
        repo-independent discovery surface (no repo is scanned). Takes no args."""
        return GroundingInfoOut.model_validate(rebar.grounding_info())

    @mcp.tool(annotations=_ANN["READ_ONLY"])
    def summary(ticket_ids: list[str]) -> list[dict]:
        """One-line-per-ticket summary [{ticket_id, status, title, blocking_summary}]."""
        return rebar.summary(*ticket_ids)

    @mcp.tool(annotations=_ANN["READ_ONLY"])
    def bridge_fsck() -> BridgeFsckOut:
        """Audit bridge mappings -> {orphaned, duplicates, stale}."""
        return BridgeFsckOut.model_validate(rebar.bridge_fsck())

    @mcp.tool(annotations=_ANN["READ_ONLY"])
    def verify_signature(ticket_id: str, kind: str | None = None) -> VerifySignatureResultOut:
        """Certify a ticket's verified-steps manifest against its signature.

        Recomputes the HMAC with THIS environment's signing key and returns
        {ticket_id, verified, verdict, reason, manifest, ...}. verdict is
        'certified' (steps match), 'mismatch' (altered/invalid), 'foreign_key'
        (signed by a different environment), or 'unsigned'. Read-only.

        `kind` selects which attestation to verify (epic dark-acme-lumen): omitted verifies
        the most-recent signature (back-compatible); an explicit kind (e.g. 'plan-review' /
        'completion-verifier') verifies that kind strictly. The full per-kind set is on the
        ticket-state `attestations` field via show_ticket."""
        return VerifySignatureResultOut.model_validate(rebar.verify_signature(ticket_id, kind=kind))

    @mcp.tool(annotations=_ANN["MUTATE_OPEN_WORLD"])
    def reconcile(mode: str = "dry-run") -> dict:
        """Run the Jira reconciler. Defaults to a non-mutating dry-run.

        The Jira-mutating modes (bootstrap-strict, bootstrap-throttle, live) each
        require REBAR_MCP_ALLOW_JIRA_SYNC=1 and are blocked under REBAR_MCP_READONLY.
        reconcile-check / dry-run are non-mutating.
        """
        # MODE_CAPS / Mode are imported once at module load (see top of file).
        # Unknown mode -> ValueError -> clean tool error.
        parsed = Mode.from_str(mode)
        # Any cap != 0 mutates Jira (10/100/None — note LIVE's cap is None, so we
        # gate on != 0, NOT > 0). cap-0 modes are non-mutating and always allowed.
        if MODE_CAPS[parsed] != 0:
            if _readonly():
                raise ValueError(
                    f"{parsed.value} reconcile is disabled: this server is "
                    "read-only (REBAR_MCP_READONLY)"
                )
            if not _allow_jira_sync():
                raise ValueError(
                    f"{parsed.value} reconcile is disabled (mutating mode); "
                    "set REBAR_MCP_ALLOW_JIRA_SYNC=1 to enable"
                )
        return rebar.reconcile(parsed.value)

    @mcp.tool(annotations=_ANN["READ_ONLY"])
    def get_workflow_status(run_id: str, ticket_id: str | None = None) -> WorkflowRunOut:
        """Read a workflow run's current status via replay (no execution) ->
        {run_id, ticket_id, workflow_name, status, terminal_step, error, steps}.

        Typed read tool (mirrors src/rebar/schemas/workflow_run.schema.json), always
        available. ``ticket_id`` is resolved from the local run index when omitted."""
        return WorkflowRunOut.model_validate(
            _cap_workflow_payload(rebar.get_workflow_status(run_id, ticket_id))
        )

    @mcp.tool(annotations=_ANN["READ_ONLY"])
    def get_workflow_result(run_id: str, ticket_id: str | None = None) -> WorkflowRunOut:
        """Read a workflow run's outputs via replay -> {run_id, status,
        terminal_step, terminal_output, outputs, error}. The terminal step's output
        is the run result.

        Typed read tool (workflow_run schema), always available. Bulky outputs are
        elided to stay under the MCP token budget (``truncated: true``); read the
        full result via the library/CLI."""
        return WorkflowRunOut.model_validate(
            _cap_workflow_payload(rebar.get_workflow_result(run_id, ticket_id))
        )

    @mcp.tool(annotations=_ANN["READ_ONLY"])
    def render_workflow(workflow: str) -> str:
        """Render a workflow (a .rebar/workflows/<name> name or a file path) to a
        read-only Mermaid flowchart (TEXT; the host renders it to SVG, never
        committed). Large graphs degrade to a text outline. Read tool, always
        available."""
        from rebar.llm.workflow import render

        return render.render_workflow(workflow)
