"""Pydantic AI output-policy adapter (RP-01 S1).

Expresses two EXISTING deterministic authorities from :mod:`rebar.llm.structured`
through documented Pydantic AI seams — adding NO retry loop, provider selection, usage
projection, or persistence. The deterministic parser and stop/refusal guard stay the sole
authorities; this module only wires them into where Pydantic AI expects a hook.

Two seams:

* :func:`output_function` — a ``TextOutput`` output function. It runs the deterministic
  :func:`rebar.llm.structured.parse_structured`. On a RETRYABLE
  :class:`~rebar.llm.errors.StructuredOutputError` it raises Pydantic AI's ``ModelRetry``
  (with the original Rebar error preserved as ``__cause__``) so Pydantic AI drives its own
  bounded output retry. A terminal :class:`~rebar.llm.errors.UnretryableOutputError` (a
  subclass) is re-raised AS-IS so it never becomes a retry prompt.
* :func:`guard_capability` — an ``AbstractCapability`` whose ``after_model_request`` hook
  runs :func:`rebar.llm.structured.check_response` on the full ``ModelResponse``. A terminal
  refusal / truncation / content-filter turn raises
  :class:`~rebar.llm.errors.UnretryableOutputError`, which propagates so the run aborts
  before the output text is processed (no retry prompt). A transient ``finish_reason='error'``
  turn raises the retryable :class:`~rebar.llm.errors.StructuredOutputError`, which is
  translated into ``ModelRetry`` so Pydantic AI drives its own bounded retry instead of
  aborting.

Pydantic AI symbols are imported lazily inside functions so top-level ``import rebar.llm``
stays stdlib-only (the ``structured_run`` convention).
"""

from __future__ import annotations

from typing import Any

from rebar.llm.errors import StructuredOutputError, UnretryableOutputError


def output_function(model_cls):
    """Return ``fn(text) -> model_cls`` for wrapping as ``pydantic_ai.TextOutput(fn)``.

    Runs the deterministic parser and translates a retryable failure into a ``ModelRetry``
    (preserving the original Rebar error as the raised exception's ``__cause__``); a
    terminal ``UnretryableOutputError`` propagates unchanged."""

    def fn(text: str):
        from pydantic_ai import ModelRetry

        from rebar.llm import structured

        try:
            return structured.parse_structured(text, model_cls)
        except UnretryableOutputError:
            raise
        except StructuredOutputError as err:
            raise ModelRetry(str(err)) from err

    return fn


def guard_capability():
    """Return an ``AbstractCapability`` applying the stop/refusal guard after each model
    request via ``check_response`` on the full ``ModelResponse``.

    A terminal :class:`~rebar.llm.errors.UnretryableOutputError` (refusal / truncation /
    content-filter) propagates unchanged so the run aborts with no retry prompt. A retryable
    :class:`~rebar.llm.errors.StructuredOutputError` (a transient ``finish_reason='error'``
    turn) is translated into Pydantic AI's ``ModelRetry`` — preserving the original Rebar
    error as ``__cause__`` — so Pydantic AI drives its own bounded retry instead of aborting
    on a non-``ModelRetry`` exception."""
    from pydantic_ai.capabilities import AbstractCapability

    class _StructuredResponseGuard(AbstractCapability):
        async def after_model_request(self, ctx, *, request_context, response) -> Any:
            from pydantic_ai import ModelRetry

            from rebar.llm import structured

            try:
                structured.check_response(response)
            except UnretryableOutputError:
                raise
            except StructuredOutputError as err:
                raise ModelRetry(str(err)) from err
            return response

    return _StructuredResponseGuard()
