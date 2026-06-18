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
from .templates import SCAFFOLD_V1, scaffold

__all__ = [
    "WorkflowError",
    "WorkflowParseError",
    "WorkflowValidationError",
    "WorkflowVersionError",
    "CURRENT_SCHEMA_VERSION",
    "SUPPORTED_SCHEMA_VERSIONS",
    "MAX_WORKFLOW_BYTES",
    "parse_workflow",
    "load_workflow",
    "validate_document",
    "declared_version",
    "schema_name_for_version",
    "migrate_to_current",
    "registered_source_versions",
    "step_kind",
    "canonical_json",
    "content_hash",
    "dump_workflow",
    "LintFinding",
    "lint_workflow",
    "lint_document",
    "lint_passes",
    "secret_scan",
    "scaffold",
    "SCAFFOLD_V1",
    "run_workflow",
    "new_run_id",
    "RunResult",
    "StepContext",
    "StepResult",
    "register_step",
    "FakeAgentRunner",
    "MemoryRecorder",
    "TicketEventRecorder",
    "sweep_orphan_snapshots",
]
