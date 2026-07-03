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

    class ListEpicsOut(_Out):
        # Mirrors src/rebar/schemas/list_epics.schema.json ({p0_bugs, epics}).
        p0_bugs: list[dict] = []
        epics: list[dict] = []

    class BridgeFsckOut(_Out):
        # Mirrors src/rebar/schemas/bridge_fsck.schema.json.
        orphaned: list = []
        duplicates: list = []
        stale: list = []

    class SignResultOut(_Out):
        # Mirrors src/rebar/schemas/sign_result.schema.json.
        ticket_id: str
        manifest: list[str] = []
        algorithm: str
        signature: str
        key_id: str
        head_sha: str
        signed_at: int

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
    ListEpicsOut = BridgeFsckOut = None  # type: ignore[assignment,misc]
    SignResultOut = VerifySignatureResultOut = None  # type: ignore[assignment,misc]
    WorkflowRunOut = None  # type: ignore[assignment,misc]
    GroundingInfoOut = GroundingBackendOut = None  # type: ignore[assignment,misc]
