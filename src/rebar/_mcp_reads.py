"""Register the MCP server's always-available read tools.

Split from ``build_server`` to keep its composition root small. The registrar
captures read-only and payload-budget helpers from ``ctx``; module-level output
models let FastMCP resolve return annotations.
"""

from __future__ import annotations

from functools import partial

import rebar
from rebar._mcp_models import (
    BridgeAccessCheckOut,
    BridgeControlOut,
    BridgeFsckOut,
    BridgeRunOut,
    BridgeStatusOut,
    ClarityResultOut,
    DepsGraphOut,
    FileImpactItemOut,
    FsckOut,
    GateResultOut,
    GateRunOut,
    GroundingInfoOut,
    NextBatchOut,
    PlanReviewStatusOut,
    ReadyTicketSummaryOut,
    SearchResultOut,
    TicketStateOut,
    ValidateReportOut,
    VerifyCommandItemOut,
    VerifyCompletionStatusOut,
    VerifySignatureResultOut,
    WorkflowRunOut,
    tool_annotation_presets,
)


def _cross_session(ticket_id: str) -> str | None:
    """The cross-session holder-naming advisory, or ``None`` if silent/uncomputable.

    Best-effort (story 734d): any exception silences the advisory rather than failing
    the read the client asked for.
    """
    from rebar._commands.cross_session import cross_session_warning_for

    try:
        return cross_session_warning_for(ticket_id, repo_root=None)
    except Exception:  # noqa: BLE001 — the advisory must never fail a read
        return None


def _gate_value(gate: object) -> bool:
    """Read either a live gate callback or its legacy boolean value."""
    return bool(gate() if callable(gate) else gate)


def _context_gate(ctx, name: str) -> bool:
    """Resolve one named gate without requiring a particular context shape."""
    return _gate_value(getattr(ctx, name))


def _register_bridge_mutation_tools(mcp, ctx, annotations) -> None:
    """Register bridge mutations on servers that expose write tools."""

    @mcp.tool(annotations=annotations["MUTATE_OPEN_WORLD"])
    def bridge_run(profile: str = "dry-run") -> BridgeRunOut:
        """Run one scheduled bridge profile and strictly deliver its ticket events."""
        if _gate_value(ctx.readonly):
            raise ValueError(
                "bridge run is disabled: this server is read-only (REBAR_MCP_READONLY)"
            )
        if not _gate_value(ctx.allow_jira_sync):
            raise ValueError("bridge run is disabled; set REBAR_MCP_ALLOW_JIRA_SYNC=1 to enable")
        return BridgeRunOut.model_validate(rebar.bridge_run(profile=profile))

    @mcp.tool(annotations=annotations["MUTATE_OPEN_WORLD"])
    def bridge_sync(
        only: list[str] | None = None,
        exclude: list[str] | None = None,
        max_changes: int | None = None,
    ) -> BridgeRunOut:
        """Apply proposed Jira changes, optionally with an explicit change limit."""
        if _gate_value(ctx.readonly):
            raise ValueError(
                "bridge sync is disabled: this server is read-only (REBAR_MCP_READONLY)"
            )
        if not _gate_value(ctx.allow_jira_sync):
            raise ValueError("bridge sync is disabled; set REBAR_MCP_ALLOW_JIRA_SYNC=1 to enable")
        values = {"only": only, "exclude": exclude, "max_changes": max_changes}
        kwargs: dict = {key: value for key, value in values.items() if value is not None}
        return BridgeRunOut.model_validate(rebar.bridge_sync(**kwargs))

    @mcp.tool(annotations=annotations["MUTATE_OPEN_WORLD"])
    def bridge_pause(reason: str) -> BridgeControlOut:
        """Persist a durable reconciliation pause with its operator reason."""
        if _gate_value(ctx.readonly):
            raise ValueError(
                "bridge pause is disabled: this server is read-only (REBAR_MCP_READONLY)"
            )
        if not _gate_value(ctx.allow_jira_sync):
            raise ValueError("bridge pause is disabled; set REBAR_MCP_ALLOW_JIRA_SYNC=1 to enable")
        return BridgeControlOut.model_validate(rebar.bridge_pause(reason=reason))

    @mcp.tool(annotations=annotations["MUTATE_OPEN_WORLD"])
    def bridge_resume() -> BridgeControlOut:
        """Clear the durable reconciliation pause."""
        if _gate_value(ctx.readonly):
            raise ValueError(
                "bridge resume is disabled: this server is read-only (REBAR_MCP_READONLY)"
            )
        if not _gate_value(ctx.allow_jira_sync):
            raise ValueError("bridge resume is disabled; set REBAR_MCP_ALLOW_JIRA_SYNC=1 to enable")
        return BridgeControlOut.model_validate(rebar.bridge_resume())


def register_bridge_tools(mcp, ctx) -> None:
    """Register additive bridge reads and the permitted mutation tools."""
    annotations = tool_annotation_presets()

    @mcp.tool(annotations=annotations["READ_ONLY"])
    def bridge_preview(
        only: list[str] | None = None, exclude: list[str] | None = None
    ) -> BridgeRunOut:
        """Compute proposed Jira changes without applying them."""
        kwargs = {
            key: value
            for key, value in {"only": only, "exclude": exclude}.items()
            if value is not None
        }
        return BridgeRunOut.model_validate(rebar.bridge_preview(**kwargs))

    @mcp.tool(annotations=annotations["READ_ONLY"])
    def bridge_status(
        target_environment_id: str | None = None,
        max_age_seconds: int | None = None,
    ) -> BridgeStatusOut:
        """Read the durable bridge status snapshot and optional freshness assertion."""
        values = {
            "target_environment_id": target_environment_id,
            "max_age_seconds": max_age_seconds,
        }
        kwargs: dict = {key: value for key, value in values.items() if value is not None}
        return BridgeStatusOut.model_validate(rebar.bridge_status(**kwargs))

    @mcp.tool(annotations=annotations["READ_ONLY_OPEN_WORLD"])
    def bridge_check_access() -> BridgeAccessCheckOut:
        """Run the six-step live Jira capability check and return its typed verdict."""
        return BridgeAccessCheckOut.model_validate(rebar.bridge_check_access())

    if not _gate_value(ctx.readonly):
        _register_bridge_mutation_tools(mcp, ctx, annotations)


def _register_plan_review_tools(mcp, annotations) -> None:
    """Register the read-only plan-review query tools.

    A module-level registrar rather than another nested ``def`` inside
    ``register_read_tools``: that function is already at its frozen
    complexity ceiling (every nested tool costs it a McCabe point), and this
    mirrors how ``register_bridge_tools`` is factored out of the same body.
    """

    @mcp.tool(annotations=annotations["READ_ONLY"])
    def plan_review_status(ticket_id: str) -> PlanReviewStatusOut:
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

        return PlanReviewStatusOut.model_validate(rebar.llm.plan_review_status(ticket_id))

    @mcp.tool(annotations=annotations["READ_ONLY"])
    def verify_completion_status(ticket_id: str) -> VerifyCompletionStatusOut:
        """Report durable completion-certificate currency without an LLM or network.

        This local close-gate check lets callers poll completion runs without another
        charge. It returns ``{ok, verdict, reason, verified_at_sha, signed_at}``;
        ``verdict`` is ``certified`` or ``unsigned``; its SHA and timestamp are
        null when no certificate exists.
        """
        import rebar.llm

        return VerifyCompletionStatusOut.model_validate(
            rebar.llm.verify_completion_status(ticket_id)
        )

    @mcp.tool(annotations=annotations["READ_ONLY"])
    def gate_status(job_id: str) -> GateRunOut:
        """Poll an async plan-review or completion run without executing it.

        Replaying the local gate index returns ``{job_id, status, ticket_id,
        gate_type, verdict?, error?, durable?, findings?}``. Status is ``running``,
        then ``passed`` or ``failed``; stale runs fail diagnostically, ``attaching``
        awaits a duplicate's index record, and ``unknown`` marks an unrecognized ID.
        ``durable`` reports signed-certificate currency. Read plan findings only after
        ``findings.readable`` is true.
        """
        import rebar.llm

        return GateRunOut.model_validate(rebar.llm.gate_run_status(job_id))


def _register_bridge_projects_read(mcp, ann) -> None:
    """Register the bridge-project read without raising the main registrar's complexity."""

    @mcp.tool(annotations=ann["READ_ONLY"])
    def bridge_projects_list() -> dict:
        """Read the store's ``{project: {"repos": [...]}}`` sync mapping without an LLM.

        Its project keys are the sync list; each value names that project's repositories.
        """
        import rebar

        return rebar.bridge_projects_list()


def _ready_discovery_row(row: dict) -> dict:
    return {
        "ticket_id": row["ticket_id"],
        "alias": row.get("alias"),
        "title": row["title"],
        "ticket_type": row["ticket_type"],
        "status": row["status"],
        "priority": row["priority"],
        "blocking_summary": "ready",
    }


def _discovery_rows(rows, *, full: bool) -> list[dict]:
    """Project ready work to the lean discovery shape unless ``full=True``.

    The default answers what is next without depending on ``TicketStateOut`` defaults;
    full mode preserves the previous complete shape.
    """
    return list(rows) if full else [_ready_discovery_row(row) for row in rows]


def register_read_tools(mcp, ctx) -> None:
    """Register the always-available read tools on ``mcp`` (see module docstring)."""
    _readonly = partial(_context_gate, ctx, "readonly")
    _cap_workflow_payload = ctx.cap_workflow_payload
    _bound_list_payload = ctx.bound_list_payload

    # ── Read tools ────────────────────────────────────────────────────────────
    _ANN = tool_annotation_presets()

    _register_bridge_projects_read(mcp, _ANN)

    @mcp.tool(annotations=_ANN["READ_ONLY"])
    def show_ticket(ticket_id: str) -> TicketStateOut:
        """Show compiled ticket state (accepts full id, short id, or alias).
        Includes the computed ``inbound_deps`` (inbound edges: other tickets
        linking TO this one, with the source's status) alongside the stored
        outgoing ``deps``."""
        from rebar.audit.read import plan_review_health

        ticket = dict(rebar.show_ticket(ticket_id, include_inbound=True))
        ticket["plan_review_health"] = plan_review_health(ticket)
        ticket["cross_session_warning"] = _cross_session(ticket_id)
        return TicketStateOut.model_validate(ticket)

    @mcp.tool(annotations=_ANN["READ_ONLY"])
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
        """List tickets with optional lifecycle, hierarchy, tag, and readiness filters.

        Archived exclusion already hides deleted tombstones; ``exclude_deleted`` matters
        with ``include_archived=True``. Rows carry ``children_count`` and can require a
        minimum. The default omits bodies and signature material; ``full=True`` restores
        complete state, while ``show_ticket`` remains the single-ticket alternative.
        Oversize results fail with structured ``response_too_large`` details and narrowing
        filters; they are never truncated or dropped by the transport.
        """
        return _bound_list_payload(
            [
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
            ],
            tool="list_tickets",
        )

    @mcp.tool(annotations=_ANN["READ_ONLY"])
    def ticket_deps(ticket_id: str) -> DepsGraphOut:
        """Show the dependency graph for a ticket."""
        return DepsGraphOut.model_validate(rebar.deps(ticket_id))

    @mcp.tool(annotations=_ANN["READ_ONLY"])
    def audit_trail(ticket_id: str) -> dict:
        """Read a ticket's complete plan, completion, and code-review audit trail.

        Plan and related code-review sidecar histories are newest-first; completion
        includes its attestation and sidecar. Individual sidecar failures degrade to
        ``[]`` or ``None``. This read remains available under ``REBAR_MCP_READONLY=1``.
        """
        from rebar.audit.read import audit_trail as _audit_trail

        return _audit_trail(ticket_id)

    @mcp.tool(annotations=_ANN["READ_ONLY"])
    def ready_tickets(
        sort: str | None = None, full: bool = False
    ) -> list[ReadyTicketSummaryOut | TicketStateOut]:
        """List tickets whose blockers are closed, optionally sorted.

        The lean default returns id, alias, title, type, status, priority, and blocking
        summary; ``full=True`` restores the pre-1.0 ``TicketStateOut`` contract, or use
        ``show_ticket`` for one item. See ``docs/release-notes.md``. Like ``list_tickets``,
        oversize results are refused; because this tool has no filters, the remedy names
        ``next_batch(epic_id)``. Sort keys are ``priority|created|updated|id|status``;
        prefix ``-`` for descending, with unset values last.
        """
        model = TicketStateOut if full else ReadyTicketSummaryOut
        return _bound_list_payload(
            [model.model_validate(t) for t in _discovery_rows(rebar.ready(sort=sort), full=full)],
            tool="ready_tickets",
        )

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
    ) -> list[SearchResultOut]:
        """Search ticket prose and tags with bounded discovery results.

        Queries support ``status:``, ``type:``, ``priority:``, ``assignee:``, ``tag:``,
        and ``parent:``; commas mean field-local OR, priorities accept comparisons and
        ``n..m``, and ``-``/``not:`` negate. Unknown fields become literal text. Sort by
        ``priority|created|updated|id|status``; prefix ``-`` for descending, with unset
        values last. Oversize results return ``response_too_large`` with count, size,
        and narrowing guidance rather than truncating.
        """
        return _bound_list_payload(
            [
                SearchResultOut.model_validate(t)
                for t in rebar.search(
                    query,
                    status=status,
                    ticket_type=ticket_type,
                    has_tag=has_tag,
                    include_archived=include_archived,
                    sort=sort,
                )
            ],
            tool="search",
        )

    @mcp.tool(annotations=_ANN["READ_ONLY"])
    def recent_session_logs(limit: int = 5) -> list[TicketStateOut]:
        """The newest session_log tickets, newest first (by created_at; default
        limit 5). session_logs are hidden from list_tickets; this is the
        type-specific read that surfaces them."""
        return [TicketStateOut.model_validate(t) for t in rebar.recent_session_logs(limit=limit)]

    @mcp.tool(annotations=_ANN["MUTATE_IDEMPOTENT"])
    def fsck(recover: bool = False) -> FsckOut:
        """Check ticket-store integrity (JSON validity, CREATE presence, lock
        cleanup). Set recover=True to run the recovery path."""
        if recover and _readonly():
            raise ValueError(
                "fsck recover=True is a write operation and is disabled: this "
                "server is read-only (REBAR_MCP_READONLY)"
            )
        # Plain fsck still mutates: it removes a stale .git/index.lock. On a
        # read-only server suppress that write (report the stale lock instead).
        return FsckOut.model_validate(rebar.fsck_report(recover=recover, report_only=_readonly()))

    # ── Quality gates + file-impact reads (WS5d) ───────────────────────────────
    @mcp.tool(annotations=_ANN["READ_ONLY"])
    def clarity_check(ticket_id: str) -> ClarityResultOut:
        """Score ticket clarity (score / verdict / threshold / passed)."""
        return ClarityResultOut.model_validate(rebar.clarity_check(ticket_id))

    @mcp.tool(annotations=_ANN["READ_ONLY"])
    def check_ac(ticket_id: str) -> GateResultOut:
        """Check the ticket has an Acceptance Criteria block
        ({verdict, criteria_count, reason, passed})."""
        result = dict(rebar.check_ac(ticket_id))
        warning = _cross_session(ticket_id)
        if warning is not None:
            result["cross_session_warning"] = warning
        return GateResultOut.model_validate(result)

    @mcp.tool(annotations=_ANN["READ_ONLY"])
    def quality_check(ticket_id: str) -> GateResultOut:
        """Check ticket dispatch readiness ({verdict, line_count, keyword_count,
        ac_items, file_impact, reason, passed})."""
        result = dict(rebar.quality_check(ticket_id))
        warning = _cross_session(ticket_id)
        if warning is not None:
            result["cross_session_warning"] = warning
        return GateResultOut.model_validate(result)

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
        """One-line-per-ticket summary [{ticket_id, alias, status, title, blocking_summary}].

        ticket_id preserves the caller token; alias is the exact resolved
        human-friendly alias, or null when resolution fails closed.
        """
        return rebar.summary(*ticket_ids)

    @mcp.tool(annotations=_ANN["READ_ONLY"])
    def bridge_fsck() -> BridgeFsckOut:
        """Offline bridge audit -> {unknown_event_types, binding_drift, store_integrity}."""
        return BridgeFsckOut.model_validate(rebar.bridge_fsck())

    register_bridge_tools(mcp, ctx)

    _register_plan_review_tools(mcp, _ANN)

    @mcp.tool(annotations=_ANN["READ_ONLY"])
    def verify_signature(ticket_id: str, kind: str | None = None) -> VerifySignatureResultOut:
        """Verify a ticket manifest against an op-cert or legacy signature.

        Returns ``{ticket_id, verified, verdict, reason, manifest, ...}``: ``certified``
        matches, ``mismatch`` is altered or invalid, ``foreign_key`` lacks an allowed
        key, and ``unsigned`` lacks a signature. Outside the opt-in environment
        restriction, signer environment alone is not a gate; ``trust_basis`` names the
        accepted key. Omitted ``kind`` checks the latest signature; an explicit
        ``plan-review`` or ``completion-verifier`` kind is strict. ``show_ticket``
        exposes every attestation.
        """
        return VerifySignatureResultOut.model_validate(rebar.verify_signature(ticket_id, kind=kind))

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
