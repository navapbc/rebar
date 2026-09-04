"""Typed output models for the rebar MCP server (FastMCP outputSchema).

Extracted from ``rebar.mcp_server`` so the per-cluster tool registrars
(``_mcp_reads`` / ``_mcp_writes`` / ``_mcp_llm``) and ``mcp_server`` itself can all
share one definition of the output models WITHOUT importing ``mcp_server`` (which
would form an import cycle). This module imports only ``pydantic`` — it is a leaf
with no ``rebar.*`` edges, so it never participates in an import cycle.

Each model mirrors a ``src/rebar/schemas/*.schema.json`` file and is kept
permissive (``extra="allow"``, non-core fields optional) so the evolving
event-sourced shapes never break a tool. FastMCP resolves a tool's return
annotation via ``eval`` against the DEFINING module's globals, so each registrar
imports the model names it annotates with at its own module level (they become
module globals there) — that is why these live at module level (not inside a
function) and why ``from __future__ import annotations`` is required so the ``|``
unions resolve on every supported Python.

The ``mcp`` extra guarantees ``pydantic``; guarded so a bare
``import rebar.mcp_server`` (or ``import rebar._mcp_models``) without the extra
still succeeds — the model names degrade to ``None`` and ``build_server`` reaches
its friendly install message before any tool is registered.
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal

try:
    from pydantic import BaseModel, ConfigDict, model_serializer

    class _Out(BaseModel):
        # Permissive base: extra fields allowed so the evolving event-sourced
        # shapes never break a tool. Each model corresponds to a src/rebar/schemas
        # file; the drift gate ``python -m rebar.schemas.check_mcp_models --check``
        # (CI-enforced) fails if a mirrored model under-declares a schema property.
        model_config = ConfigDict(extra="allow")

    class _HealthOut(_Out):
        """Preserve omitted additive fields when nested health is serialized."""

        @model_serializer(mode="wrap")
        def _serialize_only_set_fields(self, handler):
            data = handler(self)
            for name in type(self).model_fields:
                if name not in self.model_fields_set:
                    data.pop(name, None)
            return data

    class _OmitUnsetOut(_Out):
        """Drop the named fields from the serialized dump when they were never set.

        Bug 3a02: a schema property whose canonical type admits NO null — a bare
        ``string``, an enum ``$ref``, or ``const: true`` — has ABSENCE as its only
        "unset" signal.  Declaring such a property is what makes it visible in the
        published ``outputSchema``, but a declared field with a ``None`` default would
        put an explicit ``null`` on the wire, and that null makes the payload violate
        the very schema the declaration mirrors.  Listing it here keeps the declaration
        and restores absence as the unset form, so the emitted bytes are unchanged.
        """

        _omit_when_unset: ClassVar[tuple[str, ...]] = ()

        @model_serializer(mode="wrap")
        def _drop_unset_nullless_fields(self, handler):
            data = handler(self)
            for name in type(self)._omit_when_unset:
                if name not in self.model_fields_set:
                    data.pop(name, None)
            return data

    class PlanReviewHealthTargetOut(_Out):
        canonical_id: str
        role: Literal["child", "prerequisite"]
        pinned_fingerprint: str
        current_fingerprint: str | None
        pin_status: Literal["current", "stale-pin-drift", "stale-pin-missing", "malformed-pin"]

    class PlanReviewHealthAvailableOut(_HealthOut):
        available: Literal[True] = True
        valid: bool | None = None
        reason: str | None = None
        verdict: str | None = None
        pin_status: Literal[
            "current",
            "current-no-relationships",
            "stale-pin-drift",
            "stale-pin-missing",
            "malformed-pin",
            "legacy-unpinned",
        ]
        enforced: bool
        phase_status: Literal["compatible", "incompatible", "malformed"]
        signed_phase: Literal["planning", "execution"] | None
        required_phase: Literal["planning", "execution"] | None
        effective_execution_floor: float | None
        advisory: bool
        targets: list[PlanReviewHealthTargetOut]
        enforcement_status: Literal["enabled", "disabled"] | None = None
        related_material_status: (
            Literal["pinned", "no-related-material", "legacy-unpinned"] | None
        ) = None

    class PlanReviewHealthUnavailableOut(_Out):
        model_config = ConfigDict(extra="forbid")

        available: Literal[False]
        reason: Literal["derived plan-review health unavailable"]

    def _inline_schema_refs(node: Any, defs: dict[str, Any]) -> Any:
        """Inline local model refs so FastMCP advertises the nested health contract."""
        if isinstance(node, list):
            return [_inline_schema_refs(item, defs) for item in node]
        if not isinstance(node, dict):
            return node
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            name = ref.removeprefix("#/$defs/")
            target = defs.get(name, {})
            siblings = {key: value for key, value in node.items() if key != "$ref"}
            return _inline_schema_refs({**target, **siblings}, defs)
        return {key: _inline_schema_refs(value, defs) for key, value in node.items()}

    class TicketStateOut(_OmitUnsetOut):
        ticket_id: str
        ticket_type: str
        title: str
        status: str
        priority: int
        tags: list[str] = []
        assignee: str | None = None
        claimed_session: str | None = None
        claim_harness: str | None = None
        claim_remote_session: str | None = None
        parent_id: str | None = None
        alias: str | None = None
        description: str | None = None
        comments: list[dict] = []
        deps: list[dict] = []
        # Computed inbound edges (bug 05cb): {"from_id","relation","status"},
        # each meaning "from_id <relation> this ticket". Additive — `deps`
        # remains the stored outgoing-only list.
        inbound_deps: list[dict] = []
        file_impact: list[dict] = []
        file_impact_scope: Literal["undeclared", "paths", "none"] = "undeclared"
        no_file_impact_reason: str = ""
        plan_review_health: PlanReviewHealthAvailableOut | PlanReviewHealthUnavailableOut | None = (
            None
        )
        # Story 734d: the cross-session holder-naming advisory. Same reasoning as
        # description_warning — an MCP client reads only the tool result, so an
        # advisory that lives solely on the server's stderr is undeliverable here.
        # Present (non-null) only when the ticket's live claim is held by a
        # DIFFERENT session than the acting one; advisory, never a gate.
        cross_session_warning: str | None = None
        # Bug 3a02: the remaining canonical `ticket_state.schema.json` properties, now
        # DECLARED rather than riding as undeclared `extra="allow"` pass-throughs. The
        # reducer already emits every one of them, so this documents the published
        # outputSchema without changing what goes on the wire. All are optional and
        # default to null/empty — absence is meaningful (an unset provenance or close
        # field must never be reported as a fabricated value).
        author: str | None = None
        env_id: str | None = None
        bridge_project: str | None = None
        repos: list[str] = []
        verify_commands: list[dict] = []
        bridge_alerts: list = []
        reverts: list = []
        attestations: dict = {}
        preconditions_summary: dict = {}
        # Nanosecond epoch stamps. Declared `int | str` (never bare `int`) because
        # rebar._mcp_errors.js_safe_result rewrites a JS-unsafe integer as its exact
        # decimal string (bug 6fe7) and FastMCP re-validates the result against this
        # model — an `int`-only annotation would coerce it back to a lossy bare number.
        created_at: int | str | None = None
        updated_at: int | str | None = None
        last_reopened_at: int | str | None = None
        # Import provenance (P1.2): present only on tickets created by `rebar import`.
        source_id: str | None = None
        source_created_at: int | str | None = None
        source_author: str | None = None
        source_env: str | None = None
        # Which public ingress recorded the genesis CREATE, and whether that was
        # inferred for a legacy CREATE rather than stamped at genesis.
        creation_channel: str | None = None
        creation_channel_inferred: Literal[True] | None = None
        detected_by: str | None = None
        # Close disposition — present only on a closed ticket that recorded one.
        close_class: str | None = None
        close_reason: str | None = None
        force_close_reason: str | None = None
        completion_expectation: str | None = None
        #: Canonical ticket_state properties whose type admits no null — see _OmitUnsetOut.
        _omit_when_unset: ClassVar[tuple[str, ...]] = (
            "creation_channel",
            "creation_channel_inferred",
            "detected_by",
            "close_class",
            "close_reason",
            "force_close_reason",
            "completion_expectation",
        )

        @classmethod
        def model_json_schema(cls, *args: Any, **kwargs: Any) -> dict[str, Any]:
            schema = super().model_json_schema(*args, **kwargs)
            defs = schema.get("$defs", {})
            properties = schema.get("properties", {})
            health = properties.get("plan_review_health")
            if isinstance(defs, dict) and isinstance(health, dict):
                properties["plan_review_health"] = _inline_schema_refs(health, defs)
            return schema

    class SearchResultOut(_Out):
        """Bounded discovery projection; full bodies are available via show."""

        model_config = ConfigDict(extra="forbid")

        ticket_id: str
        alias: str | None
        title: str
        ticket_type: str
        status: str
        priority: int
        summary: str | None
        snippet: str | None

    class DepsGraphOut(_Out):
        ticket_id: str
        deps: list[dict] = []
        blockers: list[str] = []
        children: list[str] = []
        ready_to_work: bool

    class NextBatchOut(_OmitUnsetOut):
        # Bug 3a02: the conflict-aware batch payload, declared. `next_batch_state`
        # (the library/MCP path) ALWAYS emits every field below, so the defaults are
        # documentation rather than fill-in. `tasks` is deliberately undeclared — see
        # PERMISSIVE_OMISSIONS in rebar/schemas/check_mcp_models.py.
        epic_id: str
        epic_title: str | None = None
        batch_size: int | None = None
        available_pool: int | None = None
        batch: list[dict] = []
        skipped_overlap: list[dict] = []
        skipped_blocked: list[dict] = []
        skipped_blocked_story: list[dict] = []
        skipped_design_awaiting: list[dict] = []
        skipped_manual_awaiting: list[dict] = []
        skipped_in_progress: list[dict] = []
        skipped_needs_planning: list[dict] = []
        #: Integer/string properties the schema declares non-nullable — see _OmitUnsetOut.
        _omit_when_unset: ClassVar[tuple[str, ...]] = (
            "epic_title",
            "batch_size",
            "available_pool",
        )

    class ClarityResultOut(_Out):
        score: int
        verdict: str
        threshold: int
        passed: bool | None = None
        reason: str | None = None

    class ValidateReportOut(_Out):
        score: int
        critical_issues: list = []
        major_issues: list = []
        minor_issues: list = []
        warnings: list = []
        suggestions: list = []

    class PushStatusOut(_Out):
        """Whether this store's ticket events reached the ``sync.remote``.

        Bug ``vapoury-attack-lamb``: the tickets-branch push is best-effort and, on
        failure, WARNS — but an MCP client reads only the tool result, so a rejected push
        was undeliverable on this surface. Measured against a real declining origin, the
        ``comment_ticket`` tool returned ``{"result": "ok"}`` with two commits stranded.
        Read from the durable marker ``rebar._store.push_state`` writes, so it also
        reports a failure from a DETACHED (``sync.push=async``) push whose own stderr went
        to ``/dev/null``, and keeps reporting it on later calls until a push lands.
        ``state`` is ``"ok"`` or ``"pending"``; the rest are present only when pending.
        """

        state: str
        reason: str | None = None
        detail: str | None = None
        remote_ref: str | None = None
        unpushed: str | None = None
        since: float | None = None

    class WriteAckOut(_Out):
        """The shared ack for write tools that previously returned a bare ``"ok"``.

        FastMCP already derived ``{"result": <str>}`` from their ``-> str`` annotation, so
        this is a strict SUPERSET of the shape clients see today: ``result`` is unchanged
        and ``push_status`` is added.
        """

        result: str
        push_status: PushStatusOut | None = None
        # Ticket 594b: the save-time description-cap notice. Same reasoning as
        # push_status — an MCP client reads only the tool result, so a warning that
        # exists solely on the server's stderr is undeliverable on this surface.
        # Present (non-null) only when the write put the description over
        # verify.max_ticket_description_chars while the plan-review start-work gate
        # is enabled; advisory, and the write still succeeded.
        description_warning: str | None = None
        # Story 734d: the cross-session holder-naming advisory. Same reasoning as
        # description_warning — present (non-null) only when the ticket's live
        # claim is held by a DIFFERENT session than the acting one; advisory, and
        # the write still succeeded.
        cross_session_warning: str | None = None

    class FileImpactItemOut(_Out):
        path: str
        reason: str | None = None

    class VerifyCommandItemOut(_Out):
        command: str
        dd_id: str | None = None
        dd_text: str | None = None

    class CreateResultOut(_Out):
        id: str
        alias: str | None = None
        push_status: PushStatusOut | None = None
        #: Advisory save-time description-cap notice (ticket 594b); see WriteAckOut.
        description_warning: str | None = None
        #: Advisory create-time same-title duplicate notice (ticket eac3): non-null when
        #: another create inside the recency window carries the same normalized title —
        #: the ticket was still created; the text names the candidate and its status.
        duplicate_warning: str | None = None

    class ClaimResultOut(_Out):
        ticket_id: str
        status: str
        assignee: str | None = None
        push_status: PushStatusOut | None = None

    class GateResultOut(_OmitUnsetOut):
        # Shared by check_ac and quality_check. Bug 3a02 declares the gate-specific
        # metrics the canonical gate_result schema defines: check_ac emits
        # `criteria_count`, quality_check emits the other four, and each tool has
        # always put its own metrics on the wire as undeclared extras.
        verdict: str
        reason: str
        passed: bool | None = None
        criteria_count: int | None = None
        line_count: int | None = None
        keyword_count: int | None = None
        ac_items: int | None = None
        file_impact: int | None = None
        #: The schema types these as `integer`; the metrics the OTHER gate emits must stay
        #: ABSENT rather than become null — see _OmitUnsetOut.
        _omit_when_unset: ClassVar[tuple[str, ...]] = (
            "criteria_count",
            "line_count",
            "keyword_count",
            "ac_items",
            "file_impact",
        )

    class BridgeFsckOut(_Out):
        unknown_event_types: list[str]
        binding_drift: dict
        store_integrity: list[dict]

    class FsckOut(_Out):
        """Structured ``fsck`` report (mirrors ``src/rebar/schemas/fsck.schema.json``).

        ``returncode`` carries the COMPLETED run's exit code as data (0 = clean,
        1 = issues found); it is absent from the CLI's own ``--output json`` payload,
        hence optional here.

        ``issue_count`` is the COUNTED subset of ``issues`` — it AGREES with the exit
        code (bug 29c3-b025-04d7-454e). Each item in ``issues`` carries an additive
        ``counted`` boolean: report-only kinds (``push_pending``,
        ``status_fork_resolved``, ``tracker_dirty_tmp_event``, ``warn``) are
        ``counted=False`` and excluded from ``issue_count`` while still present in
        ``issues``, so a consumer wanting the old total can compute ``len(issues)``."""

        issues: list[dict]
        fixed: list[str]
        issue_count: int
        returncode: int | None = None
        mode: Literal["scan", "recover"] = "scan"
        report: str | None = None

    class BridgeRunOut(_Out):
        route: Literal["preview", "run", "sync"]
        state: Literal[
            "converged",
            "paused",
            "in-flight",
            "legacy-gated",
            "reschedule",
            "operational_failure",
            "invalid_invocation",
        ]
        returncode: Literal[0, 1, 2]
        details: dict[str, Any]

    class BridgeStatusOut(_HealthOut):
        verdict: Literal["HEALTHY", "PAUSED", "RUNNING", "NEVER_RUN", "FOREIGN", "FAILED", "STALE"]
        target_environment_id: str
        record_oid: str | None = None
        detail_status: Literal["missing", "mismatched", "matching"] | None = None
        pause: dict[str, Any] | None = None
        lock: dict[str, Any] | None = None
        pass_id: str | None = None
        environment_id: str | None = None
        outcome: str | None = None
        failure_kind: str | None = None
        completed_at: str | None = None
        lock_fence: int | None = None
        detail: dict[str, Any] | None = None
        lock_oid: str | None = None
        lock_holder: str | None = None
        lock_lease_secs: float | None = None
        live_lock_fence: int | None = None

    class BridgeControlOut(_HealthOut):
        state: Literal["paused", "resumed"]
        reason: str | None = None
        who: str | None = None
        paused_at: str | None = None

    class BridgeAccessStepOut(_HealthOut):
        step: Literal[
            "STEP_CREATE",
            "STEP_LABEL",
            "STEP_PROPERTY_WRITE",
            "STEP_JQL_SEARCH",
            "STEP_PROPERTY_READ",
            "STEP_DELETE",
        ]
        passed: bool
        reason: str | None = None
        detail: str | None = None

    class BridgeAccessCheckOut(_HealthOut):
        verdict: Literal["PASS", "FAIL", "INVALID"]
        steps: list[BridgeAccessStepOut]
        reason: str | None = None

    class AttachCommitsResultOut(_Out):
        """Result of ``attach_commits``: the resolved ticket and how many SHAs were recorded."""

        ticket_id: str
        attached: int

    class SignResultOut(_Out):
        # Contract phase (story 8f1d): the
        # dual-shape window is closed — `sign_manifest` mints ONLY the op-cert record, so envelope/
        # principal are required and the legacy HMAC fields (signature/key_id) are retired
        # (kept nullable only so a reader tolerates a pre-contract record).
        ticket_id: str
        manifest: list[str] = []
        algorithm: str
        signed_at: int | str  # str: JS-safe wire form (bug 6fe7)
        # Op-cert shape (always present on a freshly-minted op-cert record).
        envelope: str
        principal: str
        material_fingerprint: str | None = None
        merged_log_commit: str | None = None
        head_sha: str | None = None
        push_status: PushStatusOut | None = None
        # RETIRED legacy HMAC shape — never emitted now, nullable for pre-contract records.
        signature: str | None = None
        key_id: str | None = None

    class VerifySignatureResultOut(_Out):
        ticket_id: str
        manifest: list[str]
        step_count: int
        algorithm: str | None
        key_id: str | None
        signed_at: int | str | None  # str: JS-safe wire form (bug 6fe7)
        head_sha: str | None
        verified: bool
        verdict: str
        reason: str
        # Gate-code provenance: the rebar version+SHA that produced the attestation
        # (audit-only, epic jira-reb-596). None for pre-stamp / unsigned records.
        rebar_version: str | None = None
        # Which key certified an op-cert (bug c21f): own_key / pinned_environment /
        # envelope_key. The signing ENVIRONMENT is not a gate under current policy, so
        # this makes the weaker envelope_key basis visible rather than silent. None when
        # no trust root was reached (unsigned/legacy record, or a pre-key-selection refusal).
        trust_basis: str | None = None

    class PlanReviewStatusOut(_Out):
        # The read-only plan-review attestation currency verdict, mirroring the
        # dict rebar.llm.plan_review_status returns (the same answer the claim gate
        # would give). verified_at_sha / signed_at are None when no readable
        # certified attestation exists.
        ok: bool
        verdict: str
        reason: str
        verified_at_sha: str | None = None
        signed_at: int | str | None = None  # str: JS-safe wire form (bug 6fe7)

    class VerifyCompletionStatusOut(_Out):
        # The completion-verifier close-gate analog of PlanReviewStatusOut, mirroring
        # rebar.llm.verify_completion_status: a read-only, no-LLM attestation currency
        # read. verdict is 'certified' when a valid completion-verifier attestation
        # exists, else 'unsigned'; verified_at_sha / signed_at are None when none does.
        ok: bool
        verdict: str
        reason: str
        verified_at_sha: str | None = None
        signed_at: int | str | None = None  # str: JS-safe wire form (bug 6fe7)

    class GateRunOut(_Out):
        # The durable handle for an async gate run started by review_plan_start /
        # verify_completion_start, returned by those tools and by the gate_status poll
        # (bug d80d Phase 2). extra=allow carries the poll-only fields (verdict / error /
        # durable / findings) a settled run adds. status is running / passed / failed /
        # stale-running / attaching / unknown.
        job_id: str
        status: str
        ticket_id: str | None = None
        gate_type: str | None = None
        findings: dict[str, Any] | None = None

    class GroundingBackendOut(_Out):
        # One backend entry of GroundingInfoOut.backends.
        name: str
        available: bool
        version: str | None = None

    class GroundingInfoOut(_Out):
        # The STATIC code-grounding oracle integration contract (epic 8f6c / S5).
        dimensions_version: int
        dimensions: list[str] = []
        reference_kinds: list[str] = []
        abstain_reasons: list[str] = []
        outcomes: list[str] = []
        jobs: list[str] = []
        provenance_tiers: list[str] = []
        backends: list[GroundingBackendOut] = []

    class WorkflowRunOut(_Out):
        # One permissive model for both get_workflow_status and get_workflow_result
        # (extra=allow covers the fields each adds: steps vs outputs/terminal_output;
        # those per-call fields are the documented PERMISSIVE_OMISSIONS in the drift gate).
        run_id: str
        status: str
        ticket_id: str | None = None
        workflow_name: str | None = None

    # NOTE: transition/reopen return {ticket_id, from, to, newly_unblocked}; the
    # `from` key is a Python reserved word, so those tools return a plain dict
    # (FastMCP serializes it correctly) rather than a typed model. They therefore
    # advertise no outputSchema by design — a documented exemption pinned in
    # tests/interfaces/test_mcp_output_schema_coverage.py. Their CLI/library JSON
    # is still pinned to transition_result by test_schema_outputs.py.
except ImportError:  # pragma: no cover - pydantic ships with the mcp extra
    PlanReviewHealthTargetOut = PlanReviewHealthAvailableOut = None  # type: ignore[assignment,misc]
    PlanReviewHealthUnavailableOut = None  # type: ignore[assignment,misc]
    TicketStateOut = None  # type: ignore[assignment,misc]
    SearchResultOut = None  # type: ignore[assignment,misc]
    DepsGraphOut = ClarityResultOut = ValidateReportOut = None  # type: ignore[assignment,misc]
    NextBatchOut = FileImpactItemOut = VerifyCommandItemOut = None  # type: ignore[assignment,misc]
    CreateResultOut = ClaimResultOut = GateResultOut = None  # type: ignore[assignment,misc]
    BridgeFsckOut = None  # type: ignore[assignment,misc]
    FsckOut = None  # type: ignore[assignment,misc]
    BridgeRunOut = BridgeStatusOut = BridgeControlOut = None  # type: ignore[assignment,misc]
    BridgeAccessStepOut = BridgeAccessCheckOut = None  # type: ignore[assignment,misc]
    SignResultOut = VerifySignatureResultOut = None  # type: ignore[assignment,misc]
    AttachCommitsResultOut = None  # type: ignore[assignment,misc]
    WorkflowRunOut = None  # type: ignore[assignment,misc]
    GroundingInfoOut = GroundingBackendOut = None  # type: ignore[assignment,misc]
    PlanReviewStatusOut = None  # type: ignore[assignment,misc]
    VerifyCompletionStatusOut = GateRunOut = None  # type: ignore[assignment,misc]
    PushStatusOut = WriteAckOut = None  # type: ignore[assignment,misc]


def tool_annotation_presets() -> dict:
    """The single source of truth for MCP ``ToolAnnotations`` behavior hints, keyed
    by category, applied by the ``register_*_tools`` registrars.

    ``ToolAnnotations`` is imported LAZILY here (not at module top) so this leaf
    module stays importable WITHOUT the ``mcp`` extra — the registrars call this
    only while building the server, at which point ``mcp`` is guaranteed present.

    Hint semantics (per the MCP spec, all advisory/untrusted):
    - ``READ_ONLY`` — does not modify its environment; local.
    - ``READ_ONLY_OPEN_WORLD`` — no store mutation, but reaches an external system
      (a live LLM): the review/verify tools.
    - ``MUTATE`` — modifies the store, non-destructive, not safe to blindly repeat.
    - ``MUTATE_IDEMPOTENT`` — modifies the store but repeating with the same args is
      a no-op (tag/untag, set-* replace-semantics, fsck's stale-lock cleanup).
    - ``DESTRUCTIVE`` — modifies the store irreversibly (archive/compact).
    - ``MUTATE_OPEN_WORLD`` — may mutate AND reach an external system (bridge
      run/sync/control operations and run_workflow).
    """
    from mcp.types import ToolAnnotations

    return {
        "READ_ONLY": ToolAnnotations(readOnlyHint=True, openWorldHint=False),
        "READ_ONLY_OPEN_WORLD": ToolAnnotations(readOnlyHint=True, openWorldHint=True),
        "MUTATE": ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
        ),
        "MUTATE_IDEMPOTENT": ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
        ),
        "DESTRUCTIVE": ToolAnnotations(
            readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False
        ),
        "MUTATE_OPEN_WORLD": ToolAnnotations(readOnlyHint=False, openWorldHint=True),
    }
