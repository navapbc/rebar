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
    "SIGN_RESULT",
    "VERIFY_SIGNATURE_RESULT",
    "COMMON",
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
# signing.py — the persisted SIGNATURE record (`rebar sign`) and the uniform
# verify verdict (`rebar verify-signature`), both over `--output json`.
SIGN_RESULT = "sign_result"
VERIFY_SIGNATURE_RESULT = "verify_signature_result"

# The authoritative map of every structured (--output json / always-JSON) output
# to its schema. Keyed by command, or <command>.<interface> when an interface's
# shape adds fields (e.g. clarity_check.library adds `passed`). The coverage-guard
# test (T5) consumes this so any structured output lacking a schema fails.
OUTPUT_SCHEMAS: dict[str, str] = {
    "show": TICKET_STATE,
    "list": TICKET_STATE,
    "search": TICKET_STATE,
    "ready": TICKET_STATE,
    "show.llm": TICKET_STATE_LLM,
    "list.llm": TICKET_STATE_LLM,
    "ready.llm": TICKET_STATE_LLM,
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
    "sign": SIGN_RESULT,
    "verify_signature": VERIFY_SIGNATURE_RESULT,
    "verify_signature.not_found": ERROR_ENVELOPE,
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
