"""Structured MCP errors and shared JSON wire-shape protection.

Known rebar failures become CLI-shaped envelopes on ``ToolError.__cause__``. A guard
on the same ``mcp.tool`` registration seam converts JS-unsafe integers, including
nanosecond timestamps, to exact decimal strings. CLI JSON emitters reuse that traversal
so all JSON surfaces preserve out-of-range values.
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


# JSON interoperability guarantees only binary64-safe integers. Emit larger values,
# notably 19-digit timestamps, as exact decimal strings to avoid rounding and BigInt
# re-serialization failures.
_JS_MAX_SAFE_INT = (2**53) - 1
_JS_MIN_SAFE_INT = -((2**53) - 1)


def js_safe_result(value):
    """Recursively stringify integers outside JavaScript's exact range.

    Booleans and safe integers retain their JSON types; other scalars and concrete
    ``mcp.*`` models remain untouched. Pydantic outputs become transformed dictionaries,
    covering FastMCP's text and structured forms while preserving output validation.
    Future fields declared as ``int`` need a string annotation to avoid re-coercion.
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
    """Dump a JS-safe result while preserving each caller's JSON options.

    CLI writers use this helper; MCP transforms return values before FastMCP serializes.
    """
    return json.dumps(js_safe_result(value), **kwargs)


def install_js_safe_guard(mcp) -> None:
    """Wrap subsequently registered MCP tools with :func:`js_safe_result`.

    This composes with the error guard on the ``mcp.tool`` seam; rebinding captured
    ``call_tool`` later would not protect real transports. Install after construction
    and before tool registration.
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
