"""MCP structured-error delivery (ticket 8a31).

When an MCP tool fails with a known rebar exception, this module wraps it into an
``McpEnvelopeError`` carrying a structured ``error_envelope`` dict (the SAME shape
the CLI emits), so the driving agent can branch on machine-readable error codes
instead of parsing prose. The envelope is delivered on ``ToolError.__cause__``.
"""

from __future__ import annotations

import functools
import inspect
import json
from typing import TYPE_CHECKING

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
