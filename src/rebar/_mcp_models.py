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

try:
    from pydantic import BaseModel, ConfigDict

    class _Out(BaseModel):
        # Permissive base: extra fields allowed so the evolving event-sourced
        # shapes never break a tool. Each model mirrors a src/rebar/schemas file;
        # the cross-interface schema tests pin both to the canonical schema.
        model_config = ConfigDict(extra="allow")

    class TicketStateOut(_Out):
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
        file_impact: list[dict] = []

    class DepsGraphOut(_Out):
        ticket_id: str
        deps: list[dict] = []
        blockers: list[str] = []
        children: list[str] = []
        ready_to_work: bool

    class NextBatchOut(_Out):
        epic_id: str

    class ClarityResultOut(_Out):
        score: int
        verdict: str
        threshold: int
        passed: bool | None = None

    class ValidateReportOut(_Out):
        score: int
        critical_issues: list = []
        major_issues: list = []
        minor_issues: list = []
        warnings: list = []
        suggestions: list = []

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

    class ClaimResultOut(_Out):
        ticket_id: str
        status: str
        assignee: str | None = None

    class GateResultOut(_Out):
        verdict: str
        reason: str
        passed: bool | None = None

    class BridgeFsckOut(_Out):
        # Mirrors src/rebar/schemas/bridge_fsck.schema.json.
        orphaned: list = []
        duplicates: list = []
        stale: list = []

    class SignResultOut(_Out):
        # Mirrors src/rebar/schemas/sign_result.schema.json. Contract phase (story 8f1d): the
        # dual-shape window is closed — `sign_manifest` mints ONLY the op-cert record, so envelope/
        # principal are required and the legacy HMAC fields (signature/key_id) are retired
        # (kept nullable only so a reader tolerates a pre-contract record).
        ticket_id: str
        manifest: list[str] = []
        algorithm: str
        signed_at: int
        # Op-cert shape (always present on a freshly-minted op-cert record).
        envelope: str
        principal: str
        material_fingerprint: str | None = None
        merged_log_commit: str | None = None
        head_sha: str | None = None
        # RETIRED legacy HMAC shape — never emitted now, nullable for pre-contract records.
        signature: str | None = None
        key_id: str | None = None

    class VerifySignatureResultOut(_Out):
        # Mirrors src/rebar/schemas/verify_signature_result.schema.json.
        ticket_id: str
        manifest: list[str] = []
        step_count: int
        algorithm: str | None = None
        key_id: str | None = None
        signed_at: int | None = None
        head_sha: str | None = None
        verified: bool
        verdict: str
        reason: str
        # Gate-code provenance: the rebar version+SHA that produced the attestation
        # (audit-only, epic jira-reb-596). None for pre-stamp / unsigned records.
        rebar_version: str | None = None

    class GroundingBackendOut(_Out):
        # One backend entry of GroundingInfoOut.backends.
        name: str
        available: bool
        version: str | None = None

    class GroundingInfoOut(_Out):
        # Mirrors src/rebar/schemas/grounding_info.schema.json — the STATIC
        # code-grounding oracle integration contract (epic 8f6c / S5).
        dimensions_version: int
        dimensions: list[str] = []
        reference_kinds: list[str] = []
        abstain_reasons: list[str] = []
        outcomes: list[str] = []
        jobs: list[str] = []
        provenance_tiers: list[str] = []
        backends: list[GroundingBackendOut] = []

    class WorkflowRunOut(_Out):
        # Mirrors src/rebar/schemas/workflow_run.schema.json — one permissive model
        # for both get_workflow_status and get_workflow_result (extra=allow covers
        # the fields each adds: steps vs outputs/terminal_output).
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
    TicketStateOut = None  # type: ignore[assignment,misc]
    DepsGraphOut = ClarityResultOut = ValidateReportOut = None  # type: ignore[assignment,misc]
    NextBatchOut = FileImpactItemOut = VerifyCommandItemOut = None  # type: ignore[assignment,misc]
    CreateResultOut = ClaimResultOut = GateResultOut = None  # type: ignore[assignment,misc]
    BridgeFsckOut = None  # type: ignore[assignment,misc]
    SignResultOut = VerifySignatureResultOut = None  # type: ignore[assignment,misc]
    WorkflowRunOut = None  # type: ignore[assignment,misc]
    GroundingInfoOut = GroundingBackendOut = None  # type: ignore[assignment,misc]


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
    - ``MUTATE_OPEN_WORLD`` — may mutate AND reach an external system (run_workflow;
      reconcile in its live/bootstrap modes — annotated conservatively even though
      its default mode is a local dry run).
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
