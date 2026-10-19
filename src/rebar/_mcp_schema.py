"""Advertise the canonical JSON Schemas as the MCP tools' ``outputSchema``.

``src/rebar/schemas/*.json`` is the single output contract (story quilted-nepotic-kitty).
Tool bodies return the library payload unchanged, annotated ``-> dict[str, Any]`` or
``-> list[dict[str, Any]]``; FastMCP derives a pass-through output model for those, and
:func:`install_canonical_output_schemas` then replaces the auto-derived schema with the
canonical file's. The MCP SDK validates every structured result against the advertised
schema, so a result the canonical file does not admit fails loudly instead of drifting.

``OUTPUT_SCHEMAS_BY_TOOL`` maps each schema-backed MCP tool to its canonical schema
name(s) and to how FastMCP shapes its result. It is keyed by MCP tool name rather than
reusing :data:`rebar.schemas.OUTPUT_SCHEMAS` (keyed by CLI command) because the two
vocabularies differ (``show`` vs ``show_ticket``) and several tools share one schema;
the coverage test asserts both registries agree on every tool they share.

The installer reads three FastMCP attributes that are not public API
(``_tool_manager.get_tool``, ``fn_metadata.output_schema`` / ``output_model`` /
``wrap_output``; mcp 1.29). It asserts the invariants it relies on at registration, so an
SDK change fails at server startup rather than on the wire.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any, Literal

from rebar import schemas

Kind = Literal["object", "array", "list_of"]

_COMMON_REF_PREFIX = "common.schema.json#/$defs/"
_LOCAL_REF_PREFIX = "#/$defs/"

_WRITE_ACK_TOOLS = (
    "comment_ticket",
    "edit_ticket",
    "tag_ticket",
    "untag_ticket",
    "archive_ticket",
    "compact_ticket",
    "link_tickets",
    "unlink_tickets",
    "set_file_impact",
    "declare_no_file_impact",
    "set_verify_commands",
    "bridge_projects_set",
    "bridge_projects_remove",
)

# tool -> (canonical schema names, kind). ``object``: the one object schema as-is.
# ``array``: the canonical ARRAY schema wrapped as ``{"result": <schema>}`` (FastMCP wraps
# list returns). ``list_of``: ``{"result": {"type": "array", "items": S}}`` where S is the
# named object schema, or ``{"anyOf": [...]}`` when several are named.
OUTPUT_SCHEMAS_BY_TOOL: dict[str, tuple[tuple[str, ...], Kind]] = {
    "show_ticket": ((schemas.TICKET_STATE,), "object"),
    "list_tickets": ((schemas.TICKET_STATE,), "list_of"),
    "recent_session_logs": ((schemas.TICKET_STATE,), "list_of"),
    "search": ((schemas.SEARCH_RESULT,), "list_of"),
    # Lean discovery rows by default, full ticket_state rows with full=true.
    "ready_tickets": ((schemas.READY_TICKET_SUMMARY, schemas.TICKET_STATE), "list_of"),
    "ticket_deps": ((schemas.DEPS_GRAPH,), "object"),
    "next_batch": ((schemas.NEXT_BATCH,), "object"),
    "clarity_check": ((schemas.CLARITY_RESULT,), "object"),
    "check_ac": ((schemas.GATE_RESULT,), "object"),
    "quality_check": ((schemas.GATE_RESULT,), "object"),
    "validate": ((schemas.VALIDATE_REPORT,), "object"),
    "fsck": ((schemas.FSCK,), "object"),
    "get_file_impact": ((schemas.FILE_IMPACT,), "array"),
    "get_verify_commands": ((schemas.VERIFY_COMMANDS,), "array"),
    "summary": ((schemas.SUMMARY,), "array"),
    "grounding_info": ((schemas.GROUNDING_INFO,), "object"),
    "plan_review_status": ((schemas.PLAN_REVIEW_STATUS,), "object"),
    "verify_completion_status": ((schemas.VERIFY_COMPLETION_STATUS,), "object"),
    "gate_status": ((schemas.GATE_RUN,), "object"),
    "verify_signature": ((schemas.VERIFY_SIGNATURE_RESULT,), "object"),
    "get_workflow_status": ((schemas.WORKFLOW_RUN,), "object"),
    "get_workflow_result": ((schemas.WORKFLOW_RUN,), "object"),
    "bridge_fsck": ((schemas.BRIDGE_FSCK,), "object"),
    "bridge_preview": ((schemas.BRIDGE_RUN,), "object"),
    "bridge_run": ((schemas.BRIDGE_RUN,), "object"),
    "bridge_sync": ((schemas.BRIDGE_RUN,), "object"),
    "bridge_status": ((schemas.BRIDGE_STATUS,), "object"),
    "bridge_pause": ((schemas.BRIDGE_CONTROL,), "object"),
    "bridge_resume": ((schemas.BRIDGE_CONTROL,), "object"),
    "bridge_check_access": ((schemas.BRIDGE_ACCESS_CHECK,), "object"),
    "create_ticket": ((schemas.CREATE_RESULT,), "object"),
    "create_idea": ((schemas.CREATE_RESULT,), "object"),
    "create_identity": ((schemas.CREATE_RESULT,), "object"),
    "log_session": ((schemas.CREATE_RESULT,), "object"),
    "claim_ticket": ((schemas.CLAIM_RESULT,), "object"),
    "transition_ticket": ((schemas.TRANSITION_RESULT,), "object"),
    "reopen_ticket": ((schemas.TRANSITION_RESULT,), "object"),
    "sign_manifest": ((schemas.SIGN_RESULT,), "object"),
    "attach_commits": ((schemas.ATTACH_COMMITS_RESULT,), "object"),
    **{tool: ((schemas.WRITE_ACK,), "object") for tool in _WRITE_ACK_TOOLS},
}


def _strip_meta(schema: dict[str, Any]) -> dict[str, Any]:
    body = copy.deepcopy(schema)
    body.pop("$schema", None)
    body.pop("$id", None)
    return body


def _rewrite_refs(node: Any, wanted: set[str]) -> Any:
    """Point ``common.schema.json#/$defs/X`` refs at the local ``#/$defs/X``, collecting X."""
    if isinstance(node, list):
        return [_rewrite_refs(item, wanted) for item in node]
    if not isinstance(node, dict):
        return node
    out = {}
    for key, value in node.items():
        if key == "$ref" and isinstance(value, str):
            if value.startswith(_COMMON_REF_PREFIX):
                name = value[len(_COMMON_REF_PREFIX) :]
                wanted.add(name)
                value = _LOCAL_REF_PREFIX + name
            elif value.startswith(_LOCAL_REF_PREFIX):
                # Inside a hoisted common def, a local ref names a sibling common def.
                wanted.add(value[len(_LOCAL_REF_PREFIX) :])
            elif "#" in value or value.endswith(".json"):
                raise ValueError(f"cannot inline cross-file $ref {value!r} into an outputSchema")
        out[key] = _rewrite_refs(value, wanted)
    return out


def _hoist_common_defs(wanted: set[str]) -> dict[str, Any]:
    """Every common ``$defs`` entry ``wanted`` names, closed transitively over its own refs."""
    common_defs = schemas.load(schemas.COMMON)["$defs"]
    hoisted: dict[str, Any] = {}
    pending = sorted(wanted)
    while pending:
        name = pending.pop()
        if name in hoisted:
            continue
        nested: set[str] = set()
        hoisted[name] = _rewrite_refs(common_defs[name], nested)
        pending.extend(sorted(nested - set(hoisted)))
    return dict(sorted(hoisted.items()))


def inline_schema(names: tuple[str, ...], kind: Kind) -> dict[str, Any]:
    """The self-contained outputSchema for canonical schema ``names`` shaped by ``kind``.

    Cross-file ``common.schema.json`` refs are hoisted into local ``$defs`` (transitively)
    because the SDK's ``jsonschema.validate`` cannot resolve them; ``$schema``/``$id``
    are dropped so the advertised document is a plain, self-contained schema.
    """
    if not names or (kind != "list_of" and len(names) != 1):
        raise ValueError(f"kind {kind!r} takes exactly one schema name, got {names!r}")
    wanted: set[str] = set()
    bodies = [_rewrite_refs(_strip_meta(schemas.load(name)), wanted) for name in names]
    if kind == "object":
        out = bodies[0]
    elif kind == "array":
        out = {"type": "object", "required": ["result"], "properties": {"result": bodies[0]}}
    else:
        items = bodies[0] if len(bodies) == 1 else {"anyOf": bodies}
        out = {
            "type": "object",
            "required": ["result"],
            "properties": {"result": {"type": "array", "items": items}},
        }
    if wanted:
        if "$defs" in out:
            raise ValueError(f"schema {names!r} already carries local $defs")
        out["$defs"] = _hoist_common_defs(wanted)
    return out


def _tool_name(deco_args: tuple, deco_kwargs: dict, fn: Callable) -> str:
    name = deco_kwargs.get("name")
    if name is None and deco_args and isinstance(deco_args[0], str):
        name = deco_args[0]
    return name or fn.__name__


def install_canonical_output_schemas(mcp) -> None:
    """Rebind ``mcp.tool`` so each registered tool in the table advertises its canonical schema.

    Composes with the error and JS-safe guards on the same ``mcp.tool`` seam; install it
    after them and before tool registration. A tool whose return annotation does not give
    FastMCP the expected output shape (``kind == "object"`` must not be wrapped; ``array``
    / ``list_of`` must be) fails here, at startup.
    """
    orig_tool = mcp.tool

    def canonical_tool(*deco_args, **deco_kwargs):
        original_decorator = orig_tool(*deco_args, **deco_kwargs)

        def wrapper(fn: Callable) -> Callable:
            registered = original_decorator(fn)
            name = _tool_name(deco_args, deco_kwargs, fn)
            spec = OUTPUT_SCHEMAS_BY_TOOL.get(name)
            if spec is not None:
                names, kind = spec
                metadata = mcp._tool_manager.get_tool(name).fn_metadata
                if metadata.output_model is None:
                    raise RuntimeError(f"MCP tool {name!r} has no structured output model")
                if metadata.wrap_output != (kind != "object"):
                    raise RuntimeError(
                        f"MCP tool {name!r} result shape does not match its {kind!r} schema"
                    )
                metadata.output_schema = inline_schema(names, kind)
            return registered

        return wrapper

    mcp.tool = canonical_tool


def pydantic_typed_dict(typed_dict: type) -> Any:
    """Rebuild a ``rebar.types`` TypedDict as a ``typing_extensions.TypedDict``.

    ``rebar.types`` uses ``typing.TypedDict`` (no runtime dependency), which pydantic
    refuses on Python < 3.12. FastMCP builds tool argument models with pydantic, so an
    MCP input typed with a ``rebar.types`` entry goes through this rebuild; the keys,
    required set, and docstring are carried over unchanged.
    """
    import typing_extensions

    # Functional form: the field set is only known at runtime (ruff UP013 cannot apply).
    make_typed_dict: Any = typing_extensions.TypedDict
    rebuilt = make_typed_dict(typed_dict.__name__, dict(typed_dict.__annotations__))
    rebuilt.__doc__ = typed_dict.__doc__
    return rebuilt


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
