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
    GroundingInfoOut,
    NextBatchOut,
    PlanReviewStatusOut,
    SearchResultOut,
    TicketStateOut,
    ValidateReportOut,
    VerifyCommandItemOut,
    VerifySignatureResultOut,
    WorkflowRunOut,
    tool_annotation_presets,
)
from rebar._operation_config import _shadow


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
        _shadow("mcp.read.bridge_run")
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
        _shadow("mcp.read.bridge_sync")
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
        _shadow("mcp.read.bridge_pause")
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
        _shadow("mcp.read.bridge_resume")
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
        _shadow("mcp.read.bridge_preview")
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
        _shadow("mcp.read.bridge_status")
        values = {
            "target_environment_id": target_environment_id,
            "max_age_seconds": max_age_seconds,
        }
        kwargs: dict = {key: value for key, value in values.items() if value is not None}
        return BridgeStatusOut.model_validate(rebar.bridge_status(**kwargs))

    @mcp.tool(annotations=annotations["READ_ONLY_OPEN_WORLD"])
    def bridge_check_access() -> BridgeAccessCheckOut:
        """Run the six-step live Jira capability check and return its typed verdict."""
        _shadow("mcp.read.bridge_check_access")
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
        """Is this ticket's plan-review attestation current RIGHT NOW? Read-only.

        The MCP mirror of `rebar review-plan <id> --status`: it delegates to
        `rebar.llm.plan_review_status`, which runs the EXACT local check the claim
        gate runs — NO LLM and NO network, never a billable review — so the answer
        is precisely what a `claim` would decide.

        Returns {ok, verdict, reason, verified_at_sha, signed_at}. `verdict` is
        'certified' when current, else one of stale-code / stale-head /
        stale-material / stale-reopened / stale-pin-drift / stale-pin-missing /
        unsigned / wrong-kind / not-closed / malformed-pin / malformed-phase /
        incompatible-phase / unverifiable-material / error, and `reason` NAMES what
        changed. `verified_at_sha` is the code anchor the plan was reviewed against
        and `signed_at` the sign timestamp; both are null when no readable certified
        attestation exists. Use it to answer "should I re-gate before I implement?"
        without provoking a claim refusal."""
        _shadow("mcp.read.plan_review_status")
        import rebar.llm

        return PlanReviewStatusOut.model_validate(rebar.llm.plan_review_status(ticket_id))


def _register_bridge_projects_read(mcp, ann) -> None:
    """Register the ``bridge_projects_list`` read tool.

    Its own registrar rather than another nested ``def`` inside ``register_read_tools``:
    every nested function raises that already-large function's cyclomatic complexity, which
    the shrink-only complexity baseline gate caps.
    """

    @mcp.tool(annotations=ann["READ_ONLY"])
    def bridge_projects_list() -> dict:
        """Return the store's bridge-projects sync mapping ``{key: {"repos": [...]}}``.

        The projects key set IS the store's sync list; each entry names the repos its
        tickets belong to. A pure store READ (no LLM)."""
        _shadow("mcp.read.bridge_projects_list")
        import rebar

        return rebar.bridge_projects_list()


def register_read_tools(mcp, ctx) -> None:
    """Register the always-available read tools on ``mcp`` (see module docstring)."""
    _readonly = partial(_context_gate, ctx, "readonly")
    _allow_jira_sync = partial(_context_gate, ctx, "allow_jira_sync")
    _cap_workflow_payload = ctx.cap_workflow_payload
    MODE_CAPS = ctx.MODE_CAPS
    Mode = ctx.Mode

    # ── Read tools ────────────────────────────────────────────────────────────
    _ANN = tool_annotation_presets()

    _register_bridge_projects_read(mcp, _ANN)

    @mcp.tool(annotations=_ANN["READ_ONLY"])
    def show_ticket(ticket_id: str) -> TicketStateOut:
        """Show compiled ticket state (accepts full id, short id, or alias).
        Includes the computed ``inbound_deps`` (inbound edges: other tickets
        linking TO this one, with the source's status) alongside the stored
        outgoing ``deps``."""
        _shadow("mcp.read.show_ticket")
        from rebar.audit.read import plan_review_health

        ticket = dict(rebar.show_ticket(ticket_id, include_inbound=True))
        ticket["plan_review_health"] = plan_review_health(ticket)
        ticket["cross_session_warning"] = _cross_session(ticket_id)
        return TicketStateOut.model_validate(ticket)

    @mcp.tool(annotations=_ANN["READ_ONLY"])
    def explain_criterion(criterion_id: str) -> dict:
        """Explain a plan-review criterion — its authoring-guide section (epic cite-stone-sea /
        WS10) — OR print an author-facing prose guide when ``criterion_id`` is a guide name
        (``plan`` = how to write a passing plan; ``review`` = how to pass code review;
        ``commit-trailer`` = the required ``rebar-ticket:`` commit-trailer format). A pure
        registry/guide READ (no LLM, so it is NOT gated on REBAR_MCP_ALLOW_LLM); the SAME shared
        lookup as the `rebar explain` CLI. On failure returns a structured error
        ``{error, kind, message}`` (kind ∈ unknown-id / malformed-registry / missing-file)."""
        _shadow("mcp.read.explain_criterion")
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
        _shadow("mcp.read.list_tickets")
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
        _shadow("mcp.read.ticket_deps")
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
        _shadow("mcp.read.audit_trail")
        from rebar.audit.read import audit_trail as _audit_trail

        return _audit_trail(ticket_id)

    @mcp.tool(annotations=_ANN["READ_ONLY"])
    def ready_tickets(sort: str | None = None) -> list[TicketStateOut]:
        """List tickets ready to work (all blockers closed). ``sort`` orders by
        ``priority|created|updated|id|status`` (prefix ``-`` for descending;
        unset values sort last)."""
        _shadow("mcp.read.ready_tickets")
        return [TicketStateOut.model_validate(t) for t in rebar.ready(sort=sort)]

    @mcp.tool(annotations=_ANN["READ_ONLY"])
    def next_batch(epic_id: str) -> NextBatchOut:
        """Next parallel batch of unblocked tickets under an epic's hierarchy."""
        _shadow("mcp.read.next_batch")
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
        """Search titles/descriptions/comments/tags with bounded discovery results.

        ``query`` accepts field predicates — ``status:``/``type:``/``priority:``/
        ``assignee:``/``tag:``/``parent:`` (comma = OR within a field; ``priority``
        accepts ``<``/``<=``/``>``/``>=`` and ``n..m`` ranges) and ``-``/``not:``
        negation; an unknown ``field:`` degrades to a literal substring. ``sort``
        orders by ``priority|created|updated|id|status`` (``-`` prefix = descending;
        unset values last)."""
        _shadow("mcp.read.search")
        return [
            SearchResultOut.model_validate(t)
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
        _shadow("mcp.read.recent_session_logs")
        return [TicketStateOut.model_validate(t) for t in rebar.recent_session_logs(limit=limit)]

    @mcp.tool(annotations=_ANN["MUTATE_IDEMPOTENT"])
    def fsck(recover: bool = False) -> FsckOut:
        """Check ticket-store integrity (JSON validity, CREATE presence, lock
        cleanup). Set recover=True to run the recovery path."""
        _shadow("mcp.read.fsck")
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
        _shadow("mcp.read.clarity_check")
        return ClarityResultOut.model_validate(rebar.clarity_check(ticket_id))

    @mcp.tool(annotations=_ANN["READ_ONLY"])
    def check_ac(ticket_id: str) -> GateResultOut:
        """Check the ticket has an Acceptance Criteria block
        ({verdict, criteria_count, reason, passed})."""
        _shadow("mcp.read.check_ac")
        return GateResultOut.model_validate(rebar.check_ac(ticket_id))

    @mcp.tool(annotations=_ANN["READ_ONLY"])
    def quality_check(ticket_id: str) -> GateResultOut:
        """Check ticket dispatch readiness ({verdict, line_count, keyword_count,
        ac_items, file_impact, reason, passed})."""
        _shadow("mcp.read.quality_check")
        return GateResultOut.model_validate(rebar.quality_check(ticket_id))

    @mcp.tool(annotations=_ANN["READ_ONLY"])
    def validate() -> ValidateReportOut:
        """Repo-wide quality health check (JSON report: score, critical/major/
        minor issues, warnings, suggestions). Takes no ticket id."""
        _shadow("mcp.read.validate")
        return ValidateReportOut.model_validate(rebar.validate())

    @mcp.tool(annotations=_ANN["READ_ONLY"])
    def get_file_impact(ticket_id: str) -> list[FileImpactItemOut]:
        """Get the file-impact array (consumed by next-batch conflict scheduling)."""
        _shadow("mcp.read.get_file_impact")
        return [FileImpactItemOut.model_validate(e) for e in rebar.get_file_impact(ticket_id)]

    @mcp.tool(annotations=_ANN["READ_ONLY"])
    def get_verify_commands(ticket_id: str) -> list[VerifyCommandItemOut]:
        """Get the DD-level verify-commands array for a ticket."""
        _shadow("mcp.read.get_verify_commands")
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
        _shadow("mcp.read.grounding_info")
        return GroundingInfoOut.model_validate(rebar.grounding_info())

    @mcp.tool(annotations=_ANN["READ_ONLY"])
    def summary(ticket_ids: list[str]) -> list[dict]:
        """One-line-per-ticket summary [{ticket_id, alias, status, title, blocking_summary}].

        ticket_id preserves the caller token; alias is the exact resolved
        human-friendly alias, or null when resolution fails closed.
        """
        _shadow("mcp.read.summary")
        return rebar.summary(*ticket_ids)

    @mcp.tool(annotations=_ANN["READ_ONLY"])
    def bridge_fsck() -> BridgeFsckOut:
        """Offline bridge audit -> {unknown_event_types, binding_drift, store_integrity}."""
        _shadow("mcp.read.bridge_fsck")
        return BridgeFsckOut.model_validate(rebar.bridge_fsck())

    register_bridge_tools(mcp, ctx)

    _register_plan_review_tools(mcp, _ANN)

    @mcp.tool(annotations=_ANN["READ_ONLY"])
    def verify_signature(ticket_id: str, kind: str | None = None) -> VerifySignatureResultOut:
        """Certify a ticket's verified-steps manifest against its signature.

        Shape-aware verify (an asymmetric op-cert envelope against the signing
        environment's Ed25519 public key, or a legacy record) returning {ticket_id,
        verified, verdict, reason, manifest, ...}. verdict is 'certified' (steps
        match), 'mismatch' (altered/invalid), 'foreign_key' (signed by a different
        environment), or 'unsigned'. Read-only.

        `kind` selects which attestation to verify (epic dark-acme-lumen): omitted verifies
        the most-recent signature (back-compatible); an explicit kind (e.g. 'plan-review' /
        'completion-verifier') verifies that kind strictly. The full per-kind set is on the
        ticket-state `attestations` field via show_ticket."""
        _shadow("mcp.read.verify_signature")
        return VerifySignatureResultOut.model_validate(rebar.verify_signature(ticket_id, kind=kind))

    @mcp.tool(annotations=_ANN["MUTATE_OPEN_WORLD"])
    def reconcile(mode: str = "dry-run") -> dict:
        """Run the Jira reconciler. Defaults to a non-mutating dry-run.

        The Jira-mutating modes (bootstrap-strict, bootstrap-throttle, live) each
        require REBAR_MCP_ALLOW_JIRA_SYNC=1 and are blocked under REBAR_MCP_READONLY.
        reconcile-check / dry-run are non-mutating.
        """
        _shadow("mcp.read.reconcile")
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
        _shadow("mcp.read.get_workflow_status")
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
        _shadow("mcp.read.get_workflow_result")
        return WorkflowRunOut.model_validate(
            _cap_workflow_payload(rebar.get_workflow_result(run_id, ticket_id))
        )

    @mcp.tool(annotations=_ANN["READ_ONLY"])
    def render_workflow(workflow: str) -> str:
        """Render a workflow (a .rebar/workflows/<name> name or a file path) to a
        read-only Mermaid flowchart (TEXT; the host renders it to SVG, never
        committed). Large graphs degrade to a text outline. Read tool, always
        available."""
        _shadow("mcp.read.render_workflow")
        from rebar.llm.workflow import render

        return render.render_workflow(workflow)
