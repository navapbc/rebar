"""MCP structured-error delivery (ticket 8a31).

When an MCP tool fails with a known rebar exception, this module wraps it into an
``McpEnvelopeError`` carrying a structured ``error_envelope`` dict (the SAME shape
the CLI emits), so the driving agent can branch on machine-readable error codes
instead of parsing prose. The envelope is delivered on ``ToolError.__cause__``.

It also owns the sibling wire-shape guard (bug 6fe7): :func:`install_js_safe_guard`,
which keeps JS-unsafe integers (rebar's 19-digit nanosecond timestamps) off the
JSON-RPC wire as bare numbers. Both guards live here because they share one seam —
rebinding ``mcp.tool`` so every subsequently registered tool body is wrapped.

The wire-shape half is NOT MCP-only any more. Bug e127 extended the same rule to the
CLI ``--output json`` surface, which had the identical exposure (``jq``/``node`` round
19-digit nanosecond timestamps silently). The CLI emitters import :func:`js_safe_result`
and :func:`js_safe_dumps` from here rather than duplicating the traversal, so ONE
implementation defines rebar's JSON wire form for out-of-range integers on every surface.
The module keeps its name and every existing call site keeps its behaviour; only the set
of importers grew.
"""

from __future__ import annotations

import functools
import inspect
import json
from typing import TYPE_CHECKING

try:
    from pydantic import BaseModel
except ImportError:  # pragma: no cover - only when the `mcp` extra is absent

    class BaseModel:  # type: ignore[no-redef]
        """Stand-in so ``isinstance`` is a cheap no-match when pydantic is absent.

        Without the ``mcp`` extra no tool is ever registered, so nothing reaches the
        model branch of :func:`js_safe_result` anyway.
        """


if TYPE_CHECKING:
    from collections.abc import Callable


class McpEnvelopeError(RuntimeError):
    """An MCP tool failure carrying a structured error envelope.

    Raised by the tool guard when the body raises a known rebar exception. FastMCP
    catches this and raises ``ToolError(...) from McpEnvelopeError``, so consumers
    read the envelope off ``ToolError.__cause__.envelope``.
    """

    def __init__(self, envelope: dict) -> None:
        super().__init__(json.dumps(envelope))
        self.envelope = envelope


def _envelope_error(exc: Exception) -> McpEnvelopeError | None:
    """Return an ``McpEnvelopeError`` for a known rebar exception, else ``None``.

    ``None`` signals the caller to re-raise the original exception unchanged (it is
    not part of rebar's error vocabulary — e.g. a workflow-fence ``ValueError``).
    """
    from rebar._commands._seam import CommandError
    from rebar._config_coercion import ConfigError
    from rebar._errors import RebarError, error_code_for

    try:
        from rebar._commands.txn import ConcurrencyMismatch

        mismatch_type: type = ConcurrencyMismatch
    except ImportError:
        mismatch_type = type(None)

    try:
        from rebar.llm.errors import LLMError

        llm_error_type: type = LLMError
    except ImportError:
        llm_error_type = type(None)

    if isinstance(exc, (RebarError, CommandError, ConfigError, mismatch_type, llm_error_type)):
        from rebar._engine_support.output import error_envelope

        env = error_envelope(error_code_for(exc), "", str(exc), getattr(exc, "returncode", None))
        return McpEnvelopeError(env)
    return None


def install_error_guard(mcp) -> None:
    """Install the structured-error guard on the MCP server instance.

    Wraps ``mcp.tool`` so every subsequently registered tool is automatically guarded:
    when the body raises a known rebar exception (``RebarError`` and subclasses,
    ``ConcurrencyMismatch``, ``CommandError``, ``ConfigError``, ``LLMError``), it re-raises
    ``McpEnvelopeError`` with a structured ``error_envelope``. Async tool bodies
    (e.g. ``run_workflow``) are wrapped in an async guard so the coroutine is awaited
    and FastMCP still sees a coroutine function.

    Must be called AFTER ``mcp`` is constructed but BEFORE tool registration.
    """
    orig_tool = mcp.tool

    def guarded_tool(*deco_args, **deco_kwargs):
        """Replacement for ``mcp.tool`` that guards the decorated function."""
        original_decorator = orig_tool(*deco_args, **deco_kwargs)

        def wrapper(fn: Callable) -> Callable:
            """Wrap the tool body, preserving its signature and async-ness."""
            if inspect.iscoroutinefunction(fn):

                @functools.wraps(fn)
                async def inner_async(*fn_args, **fn_kwargs):
                    try:
                        return await fn(*fn_args, **fn_kwargs)
                    except Exception as exc:
                        envelope_error = _envelope_error(exc)
                        if envelope_error is not None:
                            raise envelope_error from exc
                        raise

                return original_decorator(inner_async)

            @functools.wraps(fn)
            def inner(*fn_args, **fn_kwargs):
                try:
                    return fn(*fn_args, **fn_kwargs)
                except Exception as exc:
                    envelope_error = _envelope_error(exc)
                    if envelope_error is not None:
                        raise envelope_error from exc
                    raise

            return original_decorator(inner)

        return wrapper

    mcp.tool = guarded_tool


# --- JS-safe integer range (bug 6fe7) -------------------------------------------------
#
# RFC 8259 s6 only guarantees interoperability for JSON numbers in the IEEE-754 binary64
# exactly-representable integer range; every supported MCP client is JavaScript and parses
# bare JSON numbers into a double. rebar stamps NANOSECOND timestamps (`created_at`,
# `updated_at`, `timestamp`, `signed_at`), which are 19 digits — far outside that range —
# so a client either SILENTLY TRUNCATES them (plain `JSON.parse`: ...032001 -> ...032000)
# or, when it parses losslessly into a BigInt (GitHub Copilot CLI), dies re-stringifying
# the tool result with `TypeError: Do not know how to serialize a BigInt`.
#
# rebar already reached this conclusion for its own store — see
# `rebar._store.canonical` and `rebar._store.hlc` ("jq must never touch it (it parses as
# float64 and rounds)") — but the MCP surface never generalized the rule. It is
# generalized here: an out-of-range integer goes on the wire as its EXACT decimal string,
# so `int(wire_value) == stored_value` holds with no rounding, scaling or dropped fields.
_JS_MAX_SAFE_INT = (2**53) - 1
_JS_MIN_SAFE_INT = -((2**53) - 1)


def js_safe_result(value):
    """Return ``value`` with every JS-unsafe integer replaced by its exact decimal string.

    Recurses through dicts, lists/tuples and pydantic models, because the implicated
    fields are nested: `TicketStateOut` declares NO timestamp fields (it inherits
    ``extra="allow"`` from ``_Out``) so `created_at`/`updated_at` pass through undeclared,
    and more hide inside raw ``list[dict]`` fields (`comments`) and inside
    ``attestations.<kind>.signed_at`` / ``signature.signed_at``.

    Deliberate non-conversions:

    * ``bool`` is a subclass of ``int`` in Python and must keep its JSON boolean type, so
      it is matched FIRST.
    * In-range integers (``priority``, counts) keep their JSON number type — only values
      outside the safe range change shape.
    * floats, ``str``, ``None`` and everything else are returned untouched.
    * ``mcp.*`` model instances (``CallToolResult``, content blocks, ``Image``) are left
      alone: FastMCP's ``_convert_to_content`` dispatches on their concrete types, so
      dumping them to dicts would change the transport shape.

    A pydantic model is returned as its dumped ``dict``. FastMCP derives BOTH the
    ``content`` text block and ``structuredContent`` from the SAME returned object
    (``convert_result`` -> ``_convert_to_content`` and ``output_model.model_validate`` in
    ``mcp/server/fastmcp/utilities/func_metadata.py``), so transforming the return value
    fixes both, and re-validating a dict against the tool's declared output model keeps
    its ``outputSchema`` contract intact.

    Caveat, deliberately out of scope: a field DECLARED as ``int`` on an output model
    would be coerced back to ``int`` by that ``model_validate``, re-emitting a bare
    number. Every field implicated in this bug is undeclared (``extra="allow"``) or lives
    inside a raw ``list[dict]``, so none is affected; a future declared 64-bit-int field
    would need its own ``str``-typed annotation.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if _JS_MIN_SAFE_INT <= value <= _JS_MAX_SAFE_INT:
            return value
        return str(value)
    if isinstance(value, dict):
        return {key: js_safe_result(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [js_safe_result(item) for item in value]
    if isinstance(value, BaseModel):
        if type(value).__module__.startswith("mcp."):
            return value
        return js_safe_result(value.model_dump())
    return value


def js_safe_dumps(value, **kwargs) -> str:
    """``json.dumps`` with every JS-unsafe integer replaced by its exact decimal string.

    The emitter-side convenience over :func:`js_safe_result`, used by the CLI
    ``--output json`` writers (bug e127) so each call site stays a single expression and
    keeps its own ``indent`` / ``separators`` / ``ensure_ascii`` / ``default`` options.
    The MCP surface does NOT go through here — it transforms the RETURN VALUE via
    :func:`install_js_safe_guard` and lets FastMCP serialize — so this wrapper adds a
    second entry point without touching the existing one.
    """
    return json.dumps(js_safe_result(value), **kwargs)


def install_js_safe_guard(mcp) -> None:
    """Install the JS-safe-integer result guard on the MCP server instance.

    Uses the SAME ``mcp.tool``-rebinding seam as :func:`install_error_guard` (and composes
    with it), so every subsequently registered tool body has its return value passed
    through :func:`js_safe_result` before FastMCP serializes it. That is the only place
    the fix can live and still hold for a real stdio/HTTP client: ``FastMCP.__init__``
    already captured ``self.call_tool`` in the lowlevel handler's closure, so rebinding
    ``mcp.call_tool`` after ``build_server()`` would work in-process and ship broken.

    Must be called AFTER ``mcp`` is constructed but BEFORE tool registration.
    """
    orig_tool = mcp.tool

    def js_safe_tool(*deco_args, **deco_kwargs):
        """Replacement for ``mcp.tool`` that sanitizes the decorated function's result."""
        original_decorator = orig_tool(*deco_args, **deco_kwargs)

        def wrapper(fn: Callable) -> Callable:
            """Wrap the tool body, preserving its signature and async-ness."""
            if inspect.iscoroutinefunction(fn):

                @functools.wraps(fn)
                async def inner_async(*fn_args, **fn_kwargs):
                    return js_safe_result(await fn(*fn_args, **fn_kwargs))

                return original_decorator(inner_async)

            @functools.wraps(fn)
            def inner(*fn_args, **fn_kwargs):
                return js_safe_result(fn(*fn_args, **fn_kwargs))

            return original_decorator(inner)

        return wrapper

    mcp.tool = js_safe_tool
