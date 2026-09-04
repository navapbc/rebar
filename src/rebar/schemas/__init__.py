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
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing-only imports (jsonschema ships with the dev extra)
    from jsonschema import Draft202012Validator
    from referencing import Registry

__all__ = [
    "BRIDGE_ACCESS_CHECK",
    "BRIDGE_CONTROL",
    "BRIDGE_FSCK",
    "BRIDGE_RUN",
    "BRIDGE_STATUS",
    "CLAIM_RESULT",
    "CLARITY_RESULT",
    "CODE_REVIEW_VERDICT",
    "COMMON",
    "COMPLETION_VERDICT",
    "CONFIG_RESULT",
    "CONTRACT_SCHEMAS",
    "CREATE_RESULT",
    "DELETE_RESULT",
    "DEPS_GRAPH",
    "DOCTOR",
    "EPIC_BUG_SCREEN_VERDICT",
    "ERROR_ENVELOPE",
    "EXPORT",
    "FETCH_TICKET_INPUT",
    "FETCH_TICKET_OUTPUT",
    "FILE_IMPACT",
    "FSCK",
    "GATE_RESULT",
    "GROUNDING",
    "GROUNDING_INFO",
    "INPUT_SCHEMAS",
    "JIRA_SNAPSHOT_ENTRY",
    "LIST_DESCENDANTS",
    "NEXT_BATCH",
    "OUTPUT_SCHEMAS",
    "OVERLAP_VERDICT",
    "PLAN_REVIEW_STATUS",
    "PLAN_REVIEW_VERDICT",
    "REVIEW_RESULT",
    "SCRATCH_ENVELOPE",
    "SEARCH_RESULT",
    "SEARCH_RESULT_LLM",
    "SIGN_RESULT",
    "SIGN_REVIEW_RESULT",
    "SUMMARY",
    "TERRAFORM_GROUNDING_RECEIPT",
    "TICKET_DIGEST",
    # name constants
    "TICKET_STATE",
    "TICKET_STATE_LLM",
    "TRACKER_FOOTPRINT",
    "TRANSITION_RESULT",
    "VALIDATE_REPORT",
    "VERIFY_COMMANDS",
    "VERIFY_SIGNATURE_RESULT",
    "WORKFLOW_RUN",
    "WORKFLOW_V1",
    "WORKFLOW_V2",
    "WORKFLOW_V3",
    "load",
    "names",
    "path",
    "registry",
    "validator",
]

COMMON = "common"
TICKET_STATE = "ticket_state"
TICKET_STATE_LLM = "ticket_state_llm"
SEARCH_RESULT = "search_result"
SEARCH_RESULT_LLM = "search_result_llm"
DEPS_GRAPH = "deps_graph"
NEXT_BATCH = "next_batch"
LIST_DESCENDANTS = "list_descendants"
CLARITY_RESULT = "clarity_result"
VALIDATE_REPORT = "validate_report"
FILE_IMPACT = "file_impact"
VERIFY_COMMANDS = "verify_commands"
SCRATCH_ENVELOPE = "scratch_envelope"
ERROR_ENVELOPE = "error_envelope"
BRIDGE_FSCK = "bridge_fsck"
BRIDGE_RUN = "bridge_run"
BRIDGE_STATUS = "bridge_status"
BRIDGE_CONTROL = "bridge_control"
BRIDGE_ACCESS_CHECK = "bridge_access_check"
CREATE_RESULT = "create_result"
CLAIM_RESULT = "claim_result"
TRANSITION_RESULT = "transition_result"
DELETE_RESULT = "delete_result"
GATE_RESULT = "gate_result"
SUMMARY = "summary"
FSCK = "fsck"
CONFIG_RESULT = "config_result"
# graph — output of `rebar doctor`: blocking edges that disagree with the
# structural link rule, plus their repair disposition after a `--repair` pass.
DOCTOR = "doctor"
# rebar.llm — the structured review-findings document. Produced by the code-review op
# (`rebar review-code` / rebar.llm.review_code) and by the single-pass review engine.
# The MCP tool is exempt (live LLM call → plain dict, no outputSchema); the CLI/library
# JSON path is pinned to this schema via the synthetic "review_code" key below.
REVIEW_RESULT = "review_result"
# rebar.llm — output of the completion-verification op (`rebar verify-completion`).
# Like review_result, the MCP tool is exempt (live LLM call → plain dict, no
# outputSchema); the CLI/library JSON path is pinned via the "verify_completion" key.
COMPLETION_VERDICT = "completion_verdict"
AUDIT_TRAIL = "audit_trail"
METRICS = "metrics"
TRACKER_FOOTPRINT = "tracker_footprint"
# `rebar verify-identity --format json` report entry (epic gnu-whale-ichor / AC7, bff8).
# verify-identity is an INTERCEPT command (not in the CLI help-arm system), so the --output
# coverage guard never drives it live; registered in OUTPUT_SCHEMAS below under a synthetic
# "verify_identity" key so the every-schema-file-is-wired guard sees verify_identity_report.
VERIFY_IDENTITY_REPORT = "verify_identity_report"
# rebar.llm — output of the Cupid ticket-digest enrichment op (epic only-crave-art,
# ee3d). No CLI --output help arm (the coverage guard never drives it live); registered
# in OUTPUT_SCHEMAS below under a synthetic "enrich" key so the every-schema-file-is-wired
# guard sees ticket_digest, and so the runner validates enrich's structured output.
TICKET_DIGEST = "ticket_digest"
# rebar.llm — output of one ordered-pair call of the Stage-2 overlap judge (epic
# only-crave-art, 9022). No CLI --output arm; registered in OUTPUT_SCHEMAS under a synthetic
# "overlap_judge" key so the schema-coverage guard sees it and the runner validates output.
OVERLAP_VERDICT = "overlap_verdict"
# rebar.llm — output of one single-turn epic-close bug-screen call (4b54). No CLI --output
# arm; registered in OUTPUT_SCHEMAS under a synthetic "epic_bug_screen" key so the
# schema-coverage guard sees it and the runner validates the screen's structured output.
EPIC_BUG_SCREEN_VERDICT = "epic_bug_screen_verdict"
# rebar.llm — output of the plan-review gate (`rebar review-plan`). The inverse of
# completion_verdict; same exemption (the MCP `review_plan` tool is NO_SCHEMA_EXEMPT
# — live LLM call → plain dict); the CLI/library JSON path is pinned via the
# "review_plan" key below.
PLAN_REVIEW_STATUS = "plan_review_status"
PLAN_REVIEW_VERDICT = "plan_review_verdict"
# rebar.llm.code_review — typed verdict of the four-pass code-review gate (epic b744),
# produced by produce_code_review_verdict. Like plan_review_verdict it is the gate's internal
# typed output (no CLI --output help arm — the public `review_code` surface returns a
# review_result); wired below under "review_code_gate" so the every-schema-file-is-wired guard
# sees it.
CODE_REVIEW_VERDICT = "code_review_verdict"
# signing.py — the persisted SIGNATURE record (`rebar sign`) and the uniform
# verify verdict (`rebar verify-signature`), both over `--output json`.
SIGN_RESULT = "sign_result"
SIGN_REVIEW_RESULT = "sign_review_result"
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
# rebar.llm.workflow — the v3 DSL schema: v2 plus the `batch` control construct (a thin
# step delegating budgeted batch orchestration over a prompt-library-backed `criteria`
# list to a runner). The current authoring version; v1/v2 files are up-converted to v3 at
# read time by the migrate shim. Like v1/v2 it is an INPUT/validation schema, exempt from
# OUTPUT_SCHEMAS via INPUT_SCHEMAS.
WORKFLOW_V3 = "workflow.v3"
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
# rebar.grounding.terraform_tools — the canonical, credential-redacting RECEIPT a
# Terraform structural grounding query emits alongside its grounding evidence (REB-640,
# slice forcible-diminished-lamb). Like GROUNDING it is an INTERNAL contract validated
# directly via schemas.validator(TERRAFORM_GROUNDING_RECEIPT), never a command --output,
# so it is exempt from OUTPUT_SCHEMAS via INPUT_SCHEMAS below.
TERRAFORM_GROUNDING_RECEIPT = "terraform_grounding_receipt"
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

# rebar_reconciler — the per-issue Jira SNAPSHOT-ENTRY contract (ADR 0004): the shape the
# fetcher (producer) writes and the inbound/outbound differs (consumers) read. An INTERNAL
# contract validated directly via schemas.validator(JIRA_SNAPSHOT_ENTRY), never a command
# --output, so (like GROUNDING) it is exempt from OUTPUT_SCHEMAS via INPUT_SCHEMAS below.
# Its generated TypedDict (rebar/types.py) puts the contract under the CI drift gate, so a
# fetcher field change has to land in the schema first — which is the enforcement ADR 0004
# always claimed and previously lacked.
JIRA_SNAPSHOT_ENTRY = "jira_snapshot_entry"

# Schemas authored to validate documents/objects directly rather than advertise a
# command's JSON output: the workflow DSL INPUT files (v1/v2), the internal grounding
# evidence CONTRACT, and the reconciler's Jira snapshot-entry CONTRACT. Like COMMON, they
# are loaded by their consumers (the workflow parser/linter; the grounding library; the
# reconciler's producer/consumer contract test) and intentionally absent from
# OUTPUT_SCHEMAS; the coverage-guard test exempts this set so an authored-but-unwired
# check still catches a forgotten OUTPUT schema while permitting these.
INPUT_SCHEMAS: frozenset[str] = frozenset(
    {
        WORKFLOW_V1,
        WORKFLOW_V2,
        WORKFLOW_V3,
        GROUNDING,
        TERRAFORM_GROUNDING_RECEIPT,
        JIRA_SNAPSHOT_ENTRY,
    }
)

# Per-step I/O CONTRACT schemas (workflow authoring v2): a step's declared input and
# output shapes, resolved by name from `@register_step`. Like INPUT_SCHEMAS these are
# consumed directly (by the inspector + linter) rather than advertised as a command's
# --output, so the coverage guard exempts them. Kept as a SEPARATE set from
# INPUT_SCHEMAS so intent reads true: these are step contracts, not DSL input files.
# One <op>_input + <op>_output pair per built-in scripted step (story e050 backfills
# the seven beyond the 5e78 fetch_ticket skeleton).
CONTRACT_SCHEMAS: frozenset[str] = frozenset(
    f"{op}_{io}"
    for op in (
        "fetch_ticket",
        "fetch_commits",
        "fetch_epic_graph",
        "overlay_triggers",
        "render_context",
        "gate",
        "comment_verdict",
        "tag",
        "set_fields",
        # epic B: the completion-gate precheck op (input + output pair).
        "completion_precheck",
        # epic B / story B2: the plan-review gate ops with an <op>_input + <op>_output pair.
        "plan_review_precheck",
        "plan_review_assemble_criteria",
        "plan_review_decide",
        # epic B / story B5: the dynamic-verify grounding op (code_grounded boolean).
        "plan_review_grounding",
        # epic b744 / WS3: the code-review gate's scripted ops (each an <op>_input + <op>_output
        # pair). assemble_diff (diff context), overlay_union (the escalation), merge_findings
        # (cluster), and the Pass-2/3/4 wiring (verify_inputs / decide / coach_inputs / coach).
        "assemble_diff",
        "overlay_union",
        "merge_findings",
        "code_review_verify_inputs",
        "code_review_decide",
        "code_review_coach_inputs",
        "code_review_coach",
        # plan-review LIVE plumbing: the verify/coach prompt-input ops that emit the
        # {{plan}} text + the verifier/coach INSTRUCTIONS listing for the LIVE prompt steps.
        "plan_review_verify_inputs",
        "plan_review_prerequisite_verify_inputs",
        "plan_review_coach_inputs",
    )
    for io in ("input", "output")
) | {
    # The shared INPUT contract for the built-in reviewer PROMPTS (their `outputs` are
    # the existing review_result / completion_verdict schemas). Permissive by design.
    "reviewer_input",
    # epic b744 / WS1: the OUTPUT contract of the code-review BASE reviewer prompt
    # (`code-review-base.md` declares `outputs: code_review_base_output`). A prompt output
    # contract consumed directly by the structured-output runner — not a command --output —
    # so, like reviewer_input, it is exempt from OUTPUT_SCHEMAS via this set.
    "code_review_base_output",
    # epic B gate ops whose OUTPUT is the existing completion_verdict schema (not an
    # <op>_output pair): only their INPUT contracts are authored here.
    "completion_reconcile_input",
    "completion_passthrough_input",
    # story B2: the plan-review ops whose OUTPUT is the existing plan_review_verdict schema
    # (not an <op>_output pair): only their INPUT contracts are authored here.
    "plan_review_coach_input",
    "plan_review_passthrough_input",
}

# The authoritative map of every structured (--output json / always-JSON) output
# to its schema. Keyed by command, or <command>.<interface> when an interface's
# shape adds fields (e.g. clarity_check.library adds `passed`). The coverage-guard
# test (T5) consumes this so any structured output lacking a schema fails.
OUTPUT_SCHEMAS: dict[str, str] = {
    "show": TICKET_STATE,
    "list": TICKET_STATE,
    "search": SEARCH_RESULT,
    "ready": TICKET_STATE,
    "session_logs": TICKET_STATE,
    "show.llm": TICKET_STATE_LLM,
    "list.llm": TICKET_STATE_LLM,
    "ready.llm": TICKET_STATE_LLM,
    "session_logs.llm": TICKET_STATE_LLM,
    "search.llm": SEARCH_RESULT_LLM,
    "deps": DEPS_GRAPH,
    "next_batch": NEXT_BATCH,
    "list_descendants": LIST_DESCENDANTS,
    "clarity_check": CLARITY_RESULT,
    "validate": VALIDATE_REPORT,
    "get_file_impact": FILE_IMPACT,
    "get_verify_commands": VERIFY_COMMANDS,
    "scratch": SCRATCH_ENVELOPE,
    "show.not_found": ERROR_ENVELOPE,
    "bridge_fsck": BRIDGE_FSCK,
    "bridge_preview": BRIDGE_RUN,
    "bridge_run": BRIDGE_RUN,
    "bridge_sync": BRIDGE_RUN,
    "bridge_status": BRIDGE_STATUS,
    "bridge_pause": BRIDGE_CONTROL,
    "bridge_resume": BRIDGE_CONTROL,
    "bridge_check_access": BRIDGE_ACCESS_CHECK,
    "create": CREATE_RESULT,
    "idea": CREATE_RESULT,
    "create_idea": CREATE_RESULT,
    "claim": CLAIM_RESULT,
    "transition": TRANSITION_RESULT,
    "reopen": TRANSITION_RESULT,
    "delete": DELETE_RESULT,
    "check_ac": GATE_RESULT,
    "quality_check": GATE_RESULT,
    "summary": SUMMARY,
    "audit": AUDIT_TRAIL,
    "metrics": METRICS,
    "tracker_footprint": TRACKER_FOOTPRINT,
    "config": CONFIG_RESULT,
    "fsck": FSCK,
    "doctor": DOCTOR,
    # Code review output uses the review result schema. CI does not execute this command
    # because it invokes a model. The driver exemption is recorded by the CLI output guard.
    "review_code": REVIEW_RESULT,
    # Specification scans share the review result shape. CI does not execute this command
    # because it invokes a model. The driver exemption is recorded by the CLI output guard.
    "scan_spec": REVIEW_RESULT,
    # Completion verification has committed CLI help and a registered schema. CI does not
    # execute the command because it invokes a model.
    "verify_completion": COMPLETION_VERDICT,
    # verify-identity merge-gate JSON report (AC7): synthetic key, no CLI help arm (the
    # --output coverage guard never drives it live); registered so the every-schema-file-is-
    # wired guard sees verify_identity_report.
    "verify_identity": VERIFY_IDENTITY_REPORT,
    # Cupid ticket-digest enrichment op (epic only-crave-art, ee3d): no CLI --output arm
    # (coverage guard never drives it live); registered so the every-schema-file-is-wired
    # guard sees ticket_digest and the runner validates enrich's structured output.
    "enrich": TICKET_DIGEST,
    # Stage-2 overlap judge (9022): synthetic key, no CLI arm; wired so the schema-coverage
    # guard sees overlap_verdict and the runner validates the judge's structured output.
    "overlap_judge": OVERLAP_VERDICT,
    # Epic-close bug screen (4b54): synthetic key, no CLI arm; wired so the schema-coverage
    # guard sees epic_bug_screen_verdict and the runner validates the screen's output.
    "epic_bug_screen": EPIC_BUG_SCREEN_VERDICT,
    # The plan review command has committed CLI help and a registered schema. CI does not
    # execute the command because it invokes a model.
    "review_plan": PLAN_REVIEW_VERDICT,
    # The re-sign operation runs without a model. Its CLI result is validated on a fixture
    # store by the output coverage guard.
    "sign_review": SIGN_REVIEW_RESULT,
    # Read-only plan-review attestation currency query (ticket 86c8): keyed by MCP tool
    # name (like get_workflow_status / grounding_info) rather than a CLI arm — the CLI
    # reaches it through `review-plan --status`, not its own subcommand, so the --output
    # coverage guard never drives it. The MCP coverage guard drives the tool on a seeded
    # store and validates the real output against this schema.
    "plan_review_status": PLAN_REVIEW_STATUS,
    # code-review gate verdict (epic b744): like review_plan, no CLI help arm (the --output
    # coverage guard never drives it live — the public review_code surface returns review_result,
    # NO_SCHEMA_EXEMPT); registered so the every-schema-file-is-wired guard sees it.
    "review_code_gate": CODE_REVIEW_VERDICT,
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


def registry() -> Registry:
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


def validator(name: str) -> Draft202012Validator:
    """A draft-2020-12 validator for ``<name>`` with the cross-file registry wired
    in. Use ``validator(name).validate(instance)`` instead of
    ``jsonschema.validate(instance, load(name))`` so ``$ref``s to common resolve.
    """
    from jsonschema import Draft202012Validator

    return Draft202012Validator(load(name), registry=registry())
