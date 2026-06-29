"""Exception hierarchy for the rebar LLM agent-operations framework.

A standalone leaf module so the package's other units (runner, operations, the
interface layers) share one error vocabulary without importing each other — and so
``import rebar.llm`` can re-export these without pulling any heavy dependency.
"""

from __future__ import annotations


class LLMError(RuntimeError):
    """Base class for all rebar.llm failures."""


class LLMUnavailableError(LLMError):
    """The LLM runtime could not run AT ALL — a SYSTEMIC failure: the ``agents`` extra
    is absent, credentials are missing/invalid, or the provider rejected/could not be
    reached (auth / connection / rate-limit). Provider-agnostic: the wrapped message
    carries whatever the provider said. The shared contract for every prompt-using
    client is that a systemic failure must surface as a FAILED/INDETERMINATE outcome —
    never be absorbed into an empty 'clean' result (the fuel-posse-ball class of bug)."""


class LLMConfigError(LLMUnavailableError):
    """A required dependency (the ``agents`` extra) or credential is missing. A kind of
    :class:`LLMUnavailableError` (so one ``except`` catches deps + runtime failures)."""


class LLMRunnerError(LLMError):
    """A runner failed to execute the operation."""


class StructuredOutputError(LLMRunnerError):
    """The agent produced no validated structured findings (see #36349) — an empty
    review must never be reported as a clean one, so this is a hard failure."""


class UnretryableOutputError(StructuredOutputError):
    """A structured-output failure that re-running the SAME call will reliably reproduce
    — a TRUNCATED turn (``stop_reason`` ``max_tokens``/``length``), a ``refusal``, or a
    ``content_filter`` block. These are complete-but-unusable turns, not a near-miss the
    model can fix when handed the validation error, so the bounded retry must FAST-FAIL on
    them instead of re-paying the full (often expensive, agentic) call 1+OUTPUT_RETRIES
    times. A subclass of :class:`StructuredOutputError`, so every existing
    ``except StructuredOutputError`` / ``except LLMError`` handler still catches it."""


class WorkflowError(LLMError):
    """Base class for the workflow engine (DSL parse/lint/migrate/execute)."""


class WorkflowParseError(WorkflowError):
    """A workflow file is not loadable: bad YAML, a rejected construct (anchor,
    merge key), an over-cap file, or not a single mapping document. Carries the
    source name and, when known, a 1-based line/column for an actionable message."""

    def __init__(
        self,
        message: str,
        *,
        source: str = "<workflow>",
        line: int | None = None,
        column: int | None = None,
    ) -> None:
        self.source = source
        self.line = line
        self.column = column
        loc = source
        if line is not None:
            loc = f"{source}:{line}" + (f":{column}" if column is not None else "")
        super().__init__(f"{loc}: {message}")


class WorkflowValidationError(WorkflowError):
    """A workflow document is loadable but fails schema/lint validation. Carries the
    full list of located, actionable findings (never just the first)."""

    def __init__(self, errors: list[str], *, source: str = "<workflow>") -> None:
        self.source = source
        self.errors = list(errors)
        joined = "\n".join(f"  - {e}" for e in self.errors)
        super().__init__(f"{source}: {len(self.errors)} validation error(s):\n{joined}")


class WorkflowVersionError(WorkflowError):
    """A workflow declares a schema_version newer than the running rebar supports —
    a hard 'upgrade rebar' error (never a best-effort parse)."""
