"""Derive the library-backed MCP tools from the annotated ``rebar.*`` signatures (ADR 0119).

Each of the 48 library-backed tools is ONE :class:`ToolSpec` in ``rebar._mcp_tool_specs`` plus
this factory. :func:`derive_tool` resolves the library function's type hints itself (with
``rebar.types`` as the local namespace, because the facade names its return TypedDicts only
under ``TYPE_CHECKING``), builds the MCP signature from the library parameters minus
``repo_root``, ``_``-prefixed names, the spec's ``strip`` and pinned names, applies the spec's
per-parameter overrides and order, and registers one generic wrapper through the already
layered ``mcp.tool`` decorator — so the error guard, the JS-safe guard, the canonical
outputSchema layer, advisory suppression, and the operation-snapshot binding all attach
unchanged. The wrapper binds the call, runs the spec's PRE hooks, calls
``getattr(rebar, spec.library)`` (resolved per call, so a patched ``rebar.<name>`` is seen),
and threads the result through the POST hooks.

Every deliberate difference from the library signature lives in the spec (``strip``,
``params``, ``order``, ``pins``) with its reason in ``divergence``; the golden surface test
pins the result byte-for-byte.
"""

from __future__ import annotations

import inspect
import sys
import typing
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

import rebar
import rebar.types

Registrar = Literal["read", "write", "bridge_read", "bridge_mutation"]

_UNSET: Any = inspect.Parameter.empty


@dataclass(frozen=True)
class Param:
    """Override one MCP parameter. ``extra=True`` adds an MCP-only parameter that the hooks
    consume and the library never receives."""

    annotation: Any = _UNSET
    default: Any = _UNSET
    extra: bool = False


@dataclass(frozen=True)
class PerCall:
    """A pinned keyword argument evaluated per call against the registrar context."""

    resolve: Callable[[Any], Any]


@dataclass
class Call:
    """One tool invocation: the library kwargs, the MCP-only extras, and hook scratch state."""

    args: dict[str, Any]
    extra: dict[str, Any] = field(default_factory=dict)
    state: dict[str, Any] = field(default_factory=dict)


PreHook = Callable[[Any, Call], None]
PostHook = Callable[[Any, Call, Any], Any]


@dataclass(frozen=True)
class ToolSpec:
    """The declaration of one derived MCP tool."""

    name: str
    library: str
    registrar: Registrar
    annotation: str
    description: str
    strip: tuple[str, ...] = ()
    params: Mapping[str, Param] = field(default_factory=dict)
    order: tuple[str, ...] = ()
    pins: Mapping[str, Any] = field(default_factory=dict)
    pre: tuple[PreHook, ...] = ()
    post: tuple[PostHook, ...] = ()
    omit_none: bool = False
    divergence: str = ""


# ── gates and context reads ─────────────────────────────────────────────────────────────
def gate_value(gate: object) -> bool:
    """Read either a live gate callback or its legacy boolean value."""
    return bool(gate() if callable(gate) else gate)


def context_gate(ctx: Any, name: str) -> bool:
    """Resolve one named gate without requiring a particular context shape."""
    return gate_value(getattr(ctx, name))


def readonly_gate(label: str) -> PreHook:
    def _pre(ctx: Any, _call: Call) -> None:
        if context_gate(ctx, "readonly"):
            raise ValueError(f"{label} is disabled: this server is read-only (REBAR_MCP_READONLY)")

    return _pre


def jira_sync_gate(label: str) -> PreHook:
    def _pre(ctx: Any, _call: Call) -> None:
        if not context_gate(ctx, "allow_jira_sync"):
            raise ValueError(f"{label} is disabled; set REBAR_MCP_ALLOW_JIRA_SYNC=1 to enable")

    return _pre


def fsck_recover_gate(ctx: Any, call: Call) -> None:
    if call.args.get("recover") and context_gate(ctx, "readonly"):
        raise ValueError(
            "fsck recover=True is a write operation and is disabled: this "
            "server is read-only (REBAR_MCP_READONLY)"
        )


def readonly_pin() -> PerCall:
    """``report_only`` follows the server's read-only gate, never the caller."""
    return PerCall(lambda ctx: context_gate(ctx, "readonly"))


# ── cross-session advisory and delivery status ──────────────────────────────────────────
def cross_session_warning(ticket_id: str) -> str | None:
    """Name another session's claim, or return ``None`` without failing the call."""
    from rebar._commands.cross_session import cross_session_warning_for

    try:
        return cross_session_warning_for(ticket_id, repo_root=None)
    except Exception:  # noqa: BLE001 — the advisory must never fail a real operation
        return None


def push_status() -> dict[str, Any]:
    """Read durable delivery state after the write; return ``unknown`` on failure.

    A detached push may fail later, so its marker appears on the next write.
    """
    try:
        return dict(rebar.push_status())
    except Exception:  # noqa: BLE001 — a status read must never fail a completed write
        return {"state": "unknown"}


def warn_before(param: str) -> PreHook:
    """Compute the advisory BEFORE the call (the write may change who holds the claim)."""

    def _pre(_ctx: Any, call: Call) -> None:
        call.state["cross_session_warning"] = cross_session_warning(call.args[param])

    return _pre


def ack(_ctx: Any, call: Call, _result: Any) -> dict[str, Any]:
    """Return write acknowledgement and delivery state, omitting silent advisories."""
    out: dict[str, Any] = {"result": call.state.get("ack_text", "ok"), "push_status": push_status()}
    for key in ("description_warning", "cross_session_warning"):
        value = call.state.get(key)
        if value is not None:
            out[key] = value
    return out


def description_warning(_ctx: Any, call: Call, result: Any) -> Any:
    call.state["description_warning"] = result
    return result


def escalation_text(_ctx: Any, call: Call, record: Any) -> Any:
    """Name both pairs when a blocking link was escalated to comparable endpoints."""
    if record:
        original = record.get("original") or {}
        resolved = record.get("resolved") or {}
        call.state["ack_text"] = (
            f"ok (escalated: {original.get('source')}->{original.get('target')} "
            f"recorded as {resolved.get('source')}->{resolved.get('target')})"
        )
    return record


def with_push(_ctx: Any, _call: Call, result: Any) -> dict[str, Any]:
    return {**dict(result), "push_status": push_status()}


def attach_warning(_ctx: Any, call: Call, result: Any) -> Any:
    """Attach a precomputed advisory only when present."""
    warning = call.state.get("cross_session_warning")
    if warning is not None:
        result["cross_session_warning"] = warning
    return result


def warn_after(param: str) -> PostHook:
    """Read surfaces compute the advisory after the read, adding the key only when set."""

    def _post(_ctx: Any, call: Call, result: Any) -> Any:
        warning = cross_session_warning(call.args[param])
        if warning is not None:
            result["cross_session_warning"] = warning
        return result

    return _post


# ── result shaping ──────────────────────────────────────────────────────────────────────
def as_dict(_ctx: Any, _call: Call, result: Any) -> dict[str, Any]:
    return dict(result)


def as_rows(_ctx: Any, _call: Call, result: Any) -> list[dict[str, Any]]:
    return [dict(row) for row in result]


def bound_list(tool: str) -> PostHook:
    """Refuse an over-budget list rather than truncate it (bug 494b)."""

    def _post(ctx: Any, _call: Call, rows: Any) -> Any:
        return ctx.bound_list_payload(rows, tool=tool)

    return _post


def plan_review_health(_ctx: Any, _call: Call, ticket: Any) -> Any:
    from rebar.audit.read import plan_review_health as _health

    ticket["plan_review_health"] = _health(ticket)
    return ticket


def _ready_discovery_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "ticket_id": row["ticket_id"],
        "alias": row.get("alias"),
        "title": row["title"],
        "ticket_type": row["ticket_type"],
        "status": row["status"],
        "priority": row["priority"],
        "blocking_summary": "ready",
    }


def discovery_rows(_ctx: Any, call: Call, rows: Any) -> list[dict[str, Any]]:
    """Project ready work to the lean discovery shape unless the MCP-only ``full`` is set
    (story 98b8: the default answers "what is next" without the full ticket_state)."""
    rows = list(rows)
    if call.extra.get("full"):
        return [dict(row) for row in rows]
    return [_ready_discovery_row(row) for row in rows]


def id_alias_only(_ctx: Any, _call: Call, result: Any) -> dict[str, Any]:
    return {"id": result["id"], "alias": result.get("alias")}


# ── the factory ─────────────────────────────────────────────────────────────────────────
#: The modules that DEFINE the ``rebar.*`` facade functions the specs derive from.
_FACADE_MODULES = (
    "rebar._lib_reads",
    "rebar._lib_writes",
    "rebar._lib_mutations",
    "rebar._lib_gates",
    "rebar._lib_ops",
    "rebar._lib_identity",
)


def facade_function(name: str) -> Callable[..., Any]:
    """The facade function ``rebar.<name>`` as DEFINED, for signature derivation.

    Resolved from its defining ``rebar._lib_*`` module rather than the ``rebar`` namespace,
    so a test that patches ``rebar.<name>`` before the server is built still derives the
    real signature (the call itself goes through ``rebar.<name>`` and sees the patch)."""
    import importlib

    for module_name in _FACADE_MODULES:
        candidate = getattr(importlib.import_module(module_name), name, None)
        if candidate is not None and getattr(candidate, "__module__", None) == module_name:
            return candidate
    return getattr(rebar, name)


def _library_hints(fn: Callable[..., Any]) -> dict[str, Any]:
    return typing.get_type_hints(
        fn, globalns=vars(sys.modules[fn.__module__]), localns=vars(rebar.types)
    )


def _return_annotation(name: str) -> Any:
    from rebar._mcp_schema import OUTPUT_SCHEMAS_BY_TOOL

    spec = OUTPUT_SCHEMAS_BY_TOOL.get(name)
    if spec is None:
        return dict  # unparameterized: FastMCP advertises no outputSchema
    return dict[str, Any] if spec[1] == "object" else list[dict[str, Any]]


def build_signature(spec: ToolSpec) -> tuple[inspect.Signature, str | None]:
    """The MCP signature for ``spec`` and the name of a library ``*args`` parameter, if any."""
    fn = facade_function(spec.library)
    hints = _library_hints(fn)
    dropped = {"repo_root", *spec.strip, *spec.pins}
    params: dict[str, inspect.Parameter] = {}
    var_positional: str | None = None
    for p in inspect.signature(fn).parameters.values():
        if p.name in dropped or p.name.startswith("_") or p.kind is p.VAR_KEYWORD:
            continue
        annotation = hints.get(p.name, Any)
        if p.kind is p.VAR_POSITIONAL:
            var_positional = p.name
            annotation = list[annotation]  # type: ignore[valid-type]
        params[p.name] = inspect.Parameter(
            p.name,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            default=p.default,
            annotation=annotation,
        )
    for name, override in spec.params.items():
        base = params.get(name)
        params[name] = inspect.Parameter(
            name,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            default=override.default if override.default is not _UNSET else base.default,  # type: ignore[union-attr]
            annotation=(
                override.annotation if override.annotation is not _UNSET else base.annotation  # type: ignore[union-attr]
            ),
        )
    ordered = [params[n] for n in spec.order] + [
        p for n, p in params.items() if n not in spec.order
    ]
    return inspect.Signature(
        ordered, return_annotation=_return_annotation(spec.name)
    ), var_positional


def _invoke(spec: ToolSpec, ctx: Any, call: Call, var_positional: str | None) -> Any:
    kwargs = dict(call.args)
    if spec.omit_none:
        kwargs = {k: v for k, v in kwargs.items() if v is not None}
    for name, value in spec.pins.items():
        kwargs[name] = value.resolve(ctx) if isinstance(value, PerCall) else value
    fn = getattr(rebar, spec.library)
    if var_positional is not None:
        return fn(*kwargs.pop(var_positional), **kwargs)
    return fn(**kwargs)


def make_tool(spec: ToolSpec, ctx: Any) -> Callable[..., Any]:
    """The one generic wrapper for ``spec``, carrying its derived signature and description."""
    sig, var_positional = build_signature(spec)
    extras = {name for name, override in spec.params.items() if override.extra}

    def tool(*args: Any, **kwargs: Any) -> Any:
        bound = sig.bind(*args, **kwargs)
        bound.apply_defaults()
        arguments = dict(bound.arguments)
        call = Call(
            args={k: v for k, v in arguments.items() if k not in extras},
            extra={k: v for k, v in arguments.items() if k in extras},
        )
        for pre in spec.pre:
            pre(ctx, call)
        result = _invoke(spec, ctx, call, var_positional)
        for post in spec.post:
            result = post(ctx, call, result)
        return result

    tool.__name__ = tool.__qualname__ = spec.name
    tool.__doc__ = spec.description
    tool.__signature__ = sig  # type: ignore[attr-defined]
    tool.__annotations__ = {
        **{p.name: p.annotation for p in sig.parameters.values()},
        "return": sig.return_annotation,
    }
    return tool


def derive_tools(mcp: Any, ctx: Any, registrar: Registrar) -> None:
    """Register every spec of ``registrar`` on ``mcp`` through its (layered) ``tool`` seam."""
    from rebar._mcp_schema import tool_annotation_presets
    from rebar._mcp_tool_specs import TOOL_SPECS

    presets = tool_annotation_presets()
    for spec in TOOL_SPECS:
        if spec.registrar == registrar:
            mcp.tool(annotations=presets[spec.annotation])(make_tool(spec, ctx))
