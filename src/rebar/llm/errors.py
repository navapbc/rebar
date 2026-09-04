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


class GateCongestedError(LLMError):
    """The host is at its concurrent-gate cap, so the gate was NOT started (ADR 0112
    decision 5; story 09da-343c-1ee9-480c).

    Deliberately NOT an :class:`LLMRunnerError`: no runner ran and nothing about the ticket
    was judged. It IS an :class:`LLMError` so that the ``except LLMError`` arms the MCP tool
    bodies and the CLI gate handlers ALREADY carry route it — a structured ``retryable``
    payload over MCP, exit 11 ("transient — retry") on the CLI — instead of a parallel except
    clause at every call site.

    Both of those paths read ``.outcome``, so the retryable disposition is attached HERE, at
    construction. Without it the MCP payload would say ``retryable: false`` and the CLI would
    exit 1: shed load that nobody retries is a dropped request, not backpressure.
    """

    def __init__(self, gate: str, limit: int) -> None:
        self.gate = gate
        self.limit = limit
        super().__init__(
            f"gate admission refused: all {limit} concurrent gate slot(s) on this host are "
            f"in use, so {gate} was not started. This is HOST CONGESTION, not a review "
            "result \u2014 no verdict was produced and nothing about the ticket was judged. "
            "Retry later, or raise [snapshot].max_concurrent_gates if the host has room."
        )
        self.outcome = _admission_outcome("gate_congested", gate, request_limit=limit)


class GateScratchUnavailableError(LLMError):
    """The gate scratch store itself is unreachable, so the gate was NOT started.

    ADR 0112 puts gate scratch on a dedicated volume and states the consequence directly: gate
    admission must treat "scratch volume unmounted" as a REFUSAL, "not as an empty cache to
    repopulate onto the root filesystem — otherwise the volume's failure mode is silently
    reverting to the state this ADR exists to prevent". So this is the one degradation in
    admission that fails CLOSED. It is retryable for the same reason the free-space floor is:
    remounting the volume clears it without changing the request.
    """

    def __init__(self, gate: str, detail: str) -> None:
        self.gate = gate
        self.detail = detail
        super().__init__(
            f"gate admission refused: the gate scratch store is unreachable ({detail}), so "
            f"{gate} was not started. Running anyway would put snapshot and clone bytes back "
            "on the root filesystem. This is a HOST condition, not a review result — no "
            "verdict was produced. Retry once the scratch volume is mounted and writable."
        )
        self.outcome = _admission_outcome("gate_scratch_unavailable", gate)


def _admission_outcome(error_type: str, gate: str, **extra: object):
    """The retryable ``LLMOutcome`` carried by an admission refusal, or ``None`` if the failure
    module is unavailable. ``WAIT_AND_RETRY`` is the honest class for both: the condition clears
    on its own (holders finish; the volume is remounted) with no change to the request. The
    import is in-body because :mod:`rebar.llm.failure` imports THIS module."""
    try:
        from rebar.llm.failure import LLMOutcome, ResolutionClass
    except ImportError:  # pragma: no cover - failure is a sibling leaf; absent only if broken
        return None
    return LLMOutcome(
        resolution_class=ResolutionClass.WAIT_AND_RETRY,
        diagnostic={"error_type": error_type, "gate": gate, **extra},
        retryable=True,
    )


class LLMRunnerError(LLMError):
    """A runner failed to execute the operation."""


class LLMBudgetExhaustedError(LLMRunnerError):
    """The agent hit its OWN step/tool-call budget (pydantic-ai ``UsageLimitExceeded``) —
    rebar stopping itself, not a provider failure or an output defect. A dedicated type
    because ``interpret_failure`` attaches the SAME diagnostic key set to every exception
    it raises, so a budget stop is identifiable ONLY by type — downstream routing (the
    completion gate's bounded per-criterion recovery) must never sniff messages or
    diagnostic shapes. A strict :class:`LLMRunnerError` subclass, so every existing
    ``except LLMRunnerError`` / ``except LLMError`` handler still catches it."""


class ContextWindowExceededError(LLMRunnerError):
    """The next request's authoritative input (system/user/tool content) plus the output
    reserve exceeds every viable candidate model's context window, so the call cannot run at
    all — rebar failing closed rather than SHORTENING authoritative input to make it fit
    (RP-01 S2). A strict :class:`LLMRunnerError` subclass, so every existing
    ``except LLMRunnerError`` / ``except LLMError`` handler still catches it."""


class LLMInputRejectedError(LLMRunnerError):
    """The provider ANSWERED and rejected the request's INPUT as unusable — an oversized
    prompt (a context-length 400, a 413), or a content-policy refusal. Deterministic and
    caller-fixable: the same input will be rejected identically every time, so waiting or
    retrying cannot help and the remedy is to SHRINK or CHANGE the input.

    Deliberately NOT an :class:`LLMUnavailableError`, for the same reason
    :class:`RunawayToolLoopError` is not: nothing is wrong with the provider, so this must
    never surface as "the LLM provider call failed" and must never be mistaken for an outage
    a caller could wait out. A strict :class:`LLMRunnerError` subclass, so every existing
    ``except LLMRunnerError`` / ``except LLMError`` handler still catches it.

    Raised only where the runner's failure seam has ALREADY classified the failure
    ``ResolutionClass.CHANGE_INPUT``; the classification is not widened here. The wrapped
    provider message rides through verbatim so the size ladder in
    ``plan_review.sizing`` can still recognise a context limit by its text.
    """


class RunawayToolLoopError(LLMRunnerError):
    """A detected repeating tool-call cycle (one signature or a k-cycle dominating the
    trailing window), aborted MID-RUN so bounded recovery can still land a verdict —
    rebar stopping itself on the repetition signal, not a provider failure or an output
    defect. A strict :class:`LLMRunnerError` subclass and deliberately NOT an
    :class:`LLMUnavailableError`: nothing is wrong with the provider, so it must never
    surface as "the LLM provider call failed". Carries a ``diagnostic`` dict like its
    siblings — bounded counters and hashed tool-call signatures only, never prompts,
    tool arguments/results, or model text, so callers may persist it in a gate-error
    sidecar."""

    def __init__(self, message: str, *, diagnostic: dict | None = None) -> None:
        super().__init__(message)
        self.diagnostic = dict(diagnostic or {})


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


class CompletionRecoveryError(StructuredOutputError):
    """Primary completion verification and its bounded recovery both failed.

    ``diagnostic`` is restricted to sanitized counters and references. It must
    never contain prompts, tool arguments/results, or model response text
    because callers may persist it in a gate-error sidecar.
    """

    def __init__(self, message: str, *, diagnostic: dict | None = None) -> None:
        super().__init__(message)
        self.diagnostic = dict(diagnostic or {})


class WorkflowError(LLMError):
    """Base class for the workflow engine (DSL parse/lint/migrate/execute)."""


class WorkflowAssetsUnavailableError(WorkflowError):
    """A local workflow editor/runtime asset needed for the command is missing."""

    error_code = "command_failed"


class WorkflowNotFoundError(WorkflowError):
    """A workflow NAME or RUN could not be resolved — a caller-input NOT-FOUND lookup miss:
    an unknown workflow name, an unknown ``run_id``, or a run absent from its ticket. A
    dedicated subclass (NOT a parse/lint failure of a workflow that WAS found, and NOT the
    bare :class:`WorkflowError` execute base) so ``error_code_for`` maps it to the caller-facing
    ``not_found`` code while an EXECUTE-time LLM outage — which surfaces on the bare base — still
    maps to ``llm_unavailable``. Optionally prefixes ``source`` onto the message so the
    workflow-name miss keeps its ``<name>: workflow '<name>' not found ...`` shape."""

    def __init__(self, message: str, *, source: str | None = None) -> None:
        self.source = source
        super().__init__(f"{source}: {message}" if source else message)


class WorkflowUnknownStepError(WorkflowError):
    """A workflow step's ``uses:`` names a scripted step absent from the step registry —
    a caller/plan-authoring fault in a workflow that WAS found (the linter validates step
    shape, not registry membership, so an unknown ``uses:`` reaches execute time). A
    dedicated subclass (NOT the bare :class:`WorkflowError` execute base) so
    ``error_code_for`` maps it to the caller-facing ``invalid_input`` code — the same class
    as :class:`WorkflowValidationError` — while a genuine EXECUTE-time LLM outage on the bare
    base still maps to ``llm_unavailable`` (dbca-97ac-ad96-4d6d AC3). Message passthrough,
    mirroring :class:`WorkflowNotFoundError`."""


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
