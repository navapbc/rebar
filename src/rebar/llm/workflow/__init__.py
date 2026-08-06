"""The rebar workflow engine: git-native, agent-editable workflows that mix
deterministic (scripted) and agentic (LLM) steps.

This package is the lean-runtime half of the engine (epic a88f). Loading,
validating, linting, migrating, and executing a workflow are pure-Python + a YAML
safe-loader — no heavy LLM dependency. Only *agentic* steps (and evals/tracing)
pull the optional ``nava-rebar[agents]`` / ``[eval]`` / ``[tracing]`` extras, and
they are imported lazily at the step boundary, never here.

Sub-modules:
  * ``schema``   — the versioned DSL: a YAML 1.2-Core-flavored safe parser, the
    immutable version-pinned JSON Schema, structural validation, deterministic
    serialization.
  * ``lint``     — reference-integrity + expression allow-list + secret scan
    (WS-B2).
  * ``migrate``  — read-time vN->v(N+1) up-conversion shim (WS-B3).

Errors live in ``rebar.llm.errors`` (the shared vocabulary): ``WorkflowError`` and
its subclasses.
"""

from __future__ import annotations

from rebar.llm.errors import (
    WorkflowError,
    WorkflowParseError,
    WorkflowValidationError,
    WorkflowVersionError,
)

from .bpmn import REBAR_MODDLE_DESCRIPTOR, bpmn_to_ir, ir_to_bpmn
from .executor import (
    FakeAgentRunner,
    MemoryRecorder,
    RunResult,
    StepContext,
    StepResult,
    TicketEventRecorder,
    new_run_id,
    register_step,
    run_workflow,
    sweep_orphan_snapshots,
)
from .lint import (
    LintFinding,
    lint_document,
    lint_passes,
    lint_workflow,
    secret_scan,
)
from .migrate import migrate_to_current, registered_source_versions
from .schema import (
    CURRENT_SCHEMA_VERSION,
    MAX_WORKFLOW_BYTES,
    SUPPORTED_SCHEMA_VERSIONS,
    canonical_json,
    content_hash,
    declared_version,
    dump_workflow,
    load_workflow,
    parse_workflow,
    schema_name_for_version,
    step_kind,
    validate_document,
)
from .snapshot import SnapshotError, resolve_sha, snapshot_at_ref

__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "MAX_WORKFLOW_BYTES",
    "REBAR_MODDLE_DESCRIPTOR",
    "SUPPORTED_SCHEMA_VERSIONS",
    "FakeAgentRunner",
    "LintFinding",
    "MemoryRecorder",
    "RunResult",
    "SnapshotError",
    "StepContext",
    "StepResult",
    "TicketEventRecorder",
    "WorkflowError",
    "WorkflowParseError",
    "WorkflowValidationError",
    "WorkflowVersionError",
    "bpmn_to_ir",
    "canonical_json",
    "content_hash",
    "declared_version",
    "dump_workflow",
    "ir_to_bpmn",
    "lint_document",
    "lint_passes",
    "lint_workflow",
    "load_workflow",
    "migrate_to_current",
    "new_run_id",
    "parse_workflow",
    "register_step",
    "registered_source_versions",
    "resolve_sha",
    "run_workflow",
    "schema_name_for_version",
    "secret_scan",
    "snapshot_at_ref",
    "step_kind",
    "sweep_orphan_snapshots",
    "validate_document",
]
