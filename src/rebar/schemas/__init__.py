"""Canonical JSON Schemas for rebar's machine-readable outputs.

These schema files are the single source of truth for the shape of rebar's JSON
outputs (e.g. the compiled ticket state from ``rebar show``). They are used to:

  * document the output contract,
  * validate real output across the CLI / library / MCP interfaces in tests, and
  * advertise output schemas to MCP clients (see ``rebar.mcp_server``).

Shared sub-objects (a comment, a dep, a {path,reason} entry, …) are authored
ONCE in ``common.schema.json`` and ``$ref``'d from the per-output schemas, so the
shapes never drift between e.g. ``get-file-impact`` and ``TicketState.file_impact``.
Because those are cross-file ``$ref``s, validate with :func:`validator` (which
wires a :mod:`referencing` registry over all schema files) rather than calling
``jsonschema.validate(instance, load(name))`` directly.

Schemas are stdlib-only package data (no runtime dependency); ``jsonschema`` and
``referencing`` are only needed to *validate* (the ``dev`` extra), not to *load*.

``OUTPUT_SCHEMAS`` maps each structured output (keyed by ``<command>`` or
``<command>.<interface>`` when an interface adds fields) to its schema name — the
single registry the coverage-guard test consumes.
"""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path
from typing import Any

__all__ = [
    "load",
    "path",
    "names",
    "registry",
    "validator",
    "OUTPUT_SCHEMAS",
    # name constants
    "TICKET_STATE",
    "TICKET_STATE_LLM",
    "DEPS_GRAPH",
    "NEXT_BATCH",
    "LIST_DESCENDANTS",
    "CLARITY_RESULT",
    "VALIDATE_REPORT",
    "BRIDGE_STATUS",
    "FILE_IMPACT",
    "VERIFY_COMMANDS",
    "SCRATCH_ENVELOPE",
    "ERROR_ENVELOPE",
    "BRIDGE_FSCK",
    "CREATE_RESULT",
    "CLAIM_RESULT",
    "TRANSITION_RESULT",
    "DELETE_RESULT",
    "GATE_RESULT",
    "SUMMARY",
    "LIST_EPICS",
    "FSCK",
    "REVIEW_RESULT",
    "COMPLETION_VERDICT",
    "SIGN_RESULT",
    "VERIFY_SIGNATURE_RESULT",
    "EXPORT",
    "COMMON",
    "WORKFLOW_V1",
    "WORKFLOW_V2",
    "WORKFLOW_RUN",
    "GROUNDING",
    "GROUNDING_INFO",
    "FETCH_TICKET_INPUT",
    "FETCH_TICKET_OUTPUT",
    "INPUT_SCHEMAS",
    "CONTRACT_SCHEMAS",
]

COMMON = "common"
TICKET_STATE = "ticket_state"
TICKET_STATE_LLM = "ticket_state_llm"
DEPS_GRAPH = "deps_graph"
NEXT_BATCH = "next_batch"
LIST_DESCENDANTS = "list_descendants"
CLARITY_RESULT = "clarity_result"
VALIDATE_REPORT = "validate_report"
BRIDGE_STATUS = "bridge_status"
FILE_IMPACT = "file_impact"
VERIFY_COMMANDS = "verify_commands"
SCRATCH_ENVELOPE = "scratch_envelope"
ERROR_ENVELOPE = "error_envelope"
BRIDGE_FSCK = "bridge_fsck"
CREATE_RESULT = "create_result"
CLAIM_RESULT = "claim_result"
TRANSITION_RESULT = "transition_result"
DELETE_RESULT = "delete_result"
GATE_RESULT = "gate_result"
SUMMARY = "summary"
LIST_EPICS = "list_epics"
FSCK = "fsck"
# rebar.llm — output of an LLM review operation (`rebar review`). The MCP tool is
# exempt (live LLM call → plain dict, no outputSchema); the CLI/library JSON path
# is pinned to this schema via the "review" key below.
REVIEW_RESULT = "review_result"
# rebar.llm — output of the completion-verification op (`rebar verify-completion`).
# Like review_result, the MCP tool is exempt (live LLM call → plain dict, no
# outputSchema); the CLI/library JSON path is pinned via the "verify_completion" key.
COMPLETION_VERDICT = "completion_verdict"
# signing.py — the persisted SIGNATURE record (`rebar sign`) and the uniform
# verify verdict (`rebar verify-signature`), both over `--output json`.
SIGN_RESULT = "sign_result"
VERIFY_SIGNATURE_RESULT = "verify_signature_result"
# _io/export_ndjson — the per-line shape of `rebar export` NDJSON output. Not in
# OUTPUT_SCHEMAS (export emits NDJSON, not the standard --output json envelope);
# documented + validated directly via schemas.validator(schemas.EXPORT).
EXPORT = "export"
# rebar.llm.workflow — the run status/result read-tool output (WS-ffc4): a typed
# read surface for get_workflow_status / get_workflow_result.
WORKFLOW_RUN = "workflow_run"
# rebar.llm.workflow — the version-pinned, IMMUTABLE workflow DSL schema (the
# `.rebar/workflows/<name>.yaml` document format). These are INPUT/validation
# schemas, not command outputs: a workflow file is validated against them via
# schemas.validator(name), and they are NOT wired into OUTPUT_SCHEMAS. Each DSL
# version is its own frozen file at a stable $id (workflow.v1, workflow.v2, …).
WORKFLOW_V1 = "workflow.v1"
# rebar.llm.workflow — the v2 DSL schema: v1 plus declarative control flow
# (branch/loop/map carrying nested frames). The current authoring version; a v1
# file is up-converted to v2 at read time by the migrate shim. Like v1 this is an
# INPUT/validation schema (a workflow file is validated against it), NOT a command
# output, so it is in INPUT_SCHEMAS and absent from OUTPUT_SCHEMAS.
WORKFLOW_V2 = "workflow.v2"
# rebar.grounding — the normalized three-valued evidence contract (epic 8f6c, story
# 0b2b). Authored as the single source of truth for the code-grounding oracle's
# evidence model and validated directly via schemas.validator(GROUNDING); it is an
# INTERNAL contract schema, NOT a command --output, so (like the workflow DSL
# schemas) it is exempt from OUTPUT_SCHEMAS via INPUT_SCHEMAS below.
GROUNDING = "grounding"
# rebar.grounding — the STATIC oracle integration contract (epic 8f6c, story S5),
# emitted by `rebar grounding-info --output json` and the `grounding_info` MCP read
# tool. Unlike the GROUNDING evidence contract (an INTERNAL schema validated
# directly), THIS is a command --output, so it IS wired into OUTPUT_SCHEMAS below.
GROUNDING_INFO = "grounding_info"
# rebar.llm.workflow — the per-step I/O CONTRACT schemas (workflow authoring v2,
# walking skeleton 5e78). A scripted step DECLARES an input + output schema BY NAME
# via `@register_step(input_schema=…, output_schema=…)`; the names resolve to these
# files through the registry. They are surfaced read-only in the editor inspector
# (CONSUMES/PRODUCES) and consumed by the linter (name-existence of a referenced
# output field). Like the workflow DSL schemas they are validated/consumed directly,
# never advertised as a command's --output, so they are exempt from OUTPUT_SCHEMAS
# via CONTRACT_SCHEMAS below.
FETCH_TICKET_INPUT = "fetch_ticket_input"
FETCH_TICKET_OUTPUT = "fetch_ticket_output"

# Schemas authored to validate documents/objects directly rather than advertise a
# command's JSON output: the workflow DSL INPUT files (v1/v2) and the internal
# grounding evidence CONTRACT. Like COMMON, they are loaded by their consumers (the
# workflow parser/linter; the grounding library) and intentionally absent from
# OUTPUT_SCHEMAS; the coverage-guard test exempts this set so an authored-but-unwired
# check still catches a forgotten OUTPUT schema while permitting these.
INPUT_SCHEMAS: frozenset[str] = frozenset({WORKFLOW_V1, WORKFLOW_V2, GROUNDING})

# Per-step I/O CONTRACT schemas (workflow authoring v2): a step's declared input and
# output shapes, resolved by name from `@register_step`. Like INPUT_SCHEMAS these are
# consumed directly (by the inspector + linter) rather than advertised as a command's
# --output, so the coverage guard exempts them. Kept as a SEPARATE set from
# INPUT_SCHEMAS so intent reads true: these are step contracts, not DSL input files.
CONTRACT_SCHEMAS: frozenset[str] = frozenset({FETCH_TICKET_INPUT, FETCH_TICKET_OUTPUT})

# The authoritative map of every structured (--output json / always-JSON) output
# to its schema. Keyed by command, or <command>.<interface> when an interface's
# shape adds fields (e.g. clarity_check.library adds `passed`). The coverage-guard
# test (T5) consumes this so any structured output lacking a schema fails.
OUTPUT_SCHEMAS: dict[str, str] = {
    "show": TICKET_STATE,
    "list": TICKET_STATE,
    "search": TICKET_STATE,
    "ready": TICKET_STATE,
    "session_logs": TICKET_STATE,
    "show.llm": TICKET_STATE_LLM,
    "list.llm": TICKET_STATE_LLM,
    "ready.llm": TICKET_STATE_LLM,
    "session_logs.llm": TICKET_STATE_LLM,
    "deps": DEPS_GRAPH,
    "next_batch": NEXT_BATCH,
    "list_descendants": LIST_DESCENDANTS,
    "clarity_check": CLARITY_RESULT,
    "validate": VALIDATE_REPORT,
    "bridge_status": BRIDGE_STATUS,
    "get_file_impact": FILE_IMPACT,
    "get_verify_commands": VERIFY_COMMANDS,
    "scratch": SCRATCH_ENVELOPE,
    "show.not_found": ERROR_ENVELOPE,
    "bridge_fsck": BRIDGE_FSCK,
    "create": CREATE_RESULT,
    "claim": CLAIM_RESULT,
    "transition": TRANSITION_RESULT,
    "reopen": TRANSITION_RESULT,
    "delete": DELETE_RESULT,
    "check_ac": GATE_RESULT,
    "quality_check": GATE_RESULT,
    "summary": SUMMARY,
    "list_epics": LIST_EPICS,
    "fsck": FSCK,
    "review": REVIEW_RESULT,
    # completion-verification op: like `review`, no CLI help arm (so the --output
    # coverage guard never drives it live) and the MCP tool is NO_SCHEMA_EXEMPT;
    # registered here so the every-schema-file-is-wired guard sees completion_verdict.
    "verify_completion": COMPLETION_VERDICT,
    "sign": SIGN_RESULT,
    "verify_signature": VERIFY_SIGNATURE_RESULT,
    "verify_signature.not_found": ERROR_ENVELOPE,
    # Workflow run status/result read tools (WS-ffc4) — both share the permissive
    # workflow_run shape. Keyed by MCP tool name; the MCP coverage guard drives
    # them on a seeded run and validates the real output against this schema.
    "get_workflow_status": WORKFLOW_RUN,
    "get_workflow_result": WORKFLOW_RUN,
    # The static code-grounding oracle integration contract (S5): a repo-independent
    # read driven by both the CLI (`grounding-info`) and the MCP `grounding_info` tool.
    "grounding_info": GROUNDING_INFO,
    # `export` emits NDJSON (one EXPORT line per ticket), not the canonical
    # --output json envelope, so it is not driven by the --output coverage guard;
    # registered here so the every-schema-file-is-wired guard sees it.
    "export": EXPORT,
}


def path(name: str) -> Path:
    """Filesystem path to the ``<name>.schema.json`` file (packaged data)."""
    return Path(str(files(__package__).joinpath(f"{name}.schema.json")))


def load(name: str) -> dict[str, Any]:
    """Parse and return the ``<name>.schema.json`` schema as a dict."""
    return json.loads(path(name).read_text(encoding="utf-8"))


def names() -> list[str]:
    """Every schema name shipped in this package (sans the ``.schema.json``)."""
    return sorted(
        p.name[: -len(".schema.json")] for p in Path(str(files(__package__))).glob("*.schema.json")
    )


def registry():
    """A :class:`referencing.Registry` over all packaged schemas, so cross-file
    ``$ref``s (e.g. ``common.schema.json#/$defs/comment``) resolve.

    Requires the ``referencing`` package (ships with ``jsonschema>=4.18``, the
    ``dev`` extra). Imported lazily so plain ``load``/``path`` stay dependency-free.
    """
    from referencing import Registry, Resource

    resources = []
    for name in names():
        schema = load(name)
        resources.append((schema["$id"], Resource.from_contents(schema)))
    return Registry().with_resources(resources)


def validator(name: str):
    """A draft-2020-12 validator for ``<name>`` with the cross-file registry wired
    in. Use ``validator(name).validate(instance)`` instead of
    ``jsonschema.validate(instance, load(name))`` so ``$ref``s to common resolve.
    """
    from jsonschema import Draft202012Validator

    return Draft202012Validator(load(name), registry=registry())
