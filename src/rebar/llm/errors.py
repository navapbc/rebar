"""Exception hierarchy for the rebar LLM agent-operations framework.

A standalone leaf module so the package's other units (runner, operations, the
interface layers) share one error vocabulary without importing each other — and so
``import rebar.llm`` can re-export these without pulling any heavy dependency.
"""

from __future__ import annotations


class LLMError(RuntimeError):
    """Base class for all rebar.llm failures."""


class LLMConfigError(LLMError):
    """A required dependency (the ``agents`` extra) or credential is missing."""


class LLMRunnerError(LLMError):
    """A runner failed to execute the operation."""


class StructuredOutputError(LLMRunnerError):
    """The agent produced no validated structured findings (see #36349) — an empty
    review must never be reported as a clean one, so this is a hard failure."""
