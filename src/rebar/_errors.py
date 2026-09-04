"""Library exception types (stdlib-only leaf).

``RebarError`` and its ``ConcurrencyError`` subclass live here — a **top-of-tree
leaf** that imports only stdlib — so any module can raise/catch them without
reaching UP into the ``rebar`` facade (``rebar/__init__.py``).

Historically these were defined in ``rebar/__init__.py``; the read facade
``rebar._reads`` had to reach back UP into it via a function-local
``from rebar import RebarError``, which kept ``_reads`` inside the large import
SCC. Moving the types to this leaf lets ``_reads`` (and any other reader) source
them downward, removing that back-edge (item 9.3). ``rebar/__init__.py``
re-exports both names, so ``rebar.RebarError`` / ``from rebar import RebarError``
are unchanged.
"""

from __future__ import annotations


class RebarError(RuntimeError):
    """A rebar engine command failed."""

    def __init__(self, message: str, *, returncode: int = 1, stderr: str = ""):
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr


class TrackerRootError(RebarError):
    """The read path could not resolve a git-backed repo root (bug 176d).

    Raised by ``_engine_support.reads.tracker_dir`` instead of ``sys.exit``: that
    helper is reached from the library gate surface and thence from MCP, where a
    ``SystemExit`` (a ``BaseException``) sails past FastMCP's ``except Exception``
    and kills the server process. The CLI maps this back to exit 1 with the same
    stderr line it always printed, so its observable contract is unchanged.
    """


class ConcurrencyError(RebarError):
    """Optimistic-concurrency rejection (the ticket changed since it was read).

    Raised by :func:`rebar.transition` when the engine reports exit code 10.
    """


# ── Error classification vocabulary (ticket 8a31) ──────────────────────────────

KNOWN_ERROR_CODES: frozenset[str] = frozenset(
    {
        "ticket_not_found",
        "pinned_ticket_read_failed",
        "store_uninitialized",
        "show_failed",
        "deps_failed",
        "concurrency_conflict",
        "claim_failed",
        "invalid_ticket_type",
        "tracker_root_unresolved",
        "criterion_unknown_id",
        "criterion_registry_malformed",
        "criterion_missing_file",
        "llm_unavailable",
        "gate_congested",
        "gate_scratch_unavailable",
        "not_found",
        "invalid_input",
        "config_unreadable",
        "config_insecure_url",
        "response_too_large",
        "command_failed",
    }
)


def _workflow_caller_code(exc: BaseException) -> str | None:
    """Classify a workflow-engine caller-input error to its vocabulary code, else ``None``.

    Extracted from :func:`error_code_for` (bug dbca-97ac-ad96-4d6d) so the classifier stays
    under the per-function complexity ceiling. ``WorkflowNotFoundError`` (an unknown workflow
    name or ``run_id``) is a caller ``not_found``; a ``WorkflowParseError`` /
    ``WorkflowValidationError`` / ``WorkflowVersionError`` / ``WorkflowUnknownStepError`` (a
    workflow that WAS found but will not parse/lint/migrate, or names an unknown scripted step)
    is caller ``invalid_input``. Returns ``None`` for the bare ``WorkflowError`` base so its
    EXECUTE-time LLM outage still resolves to ``llm_unavailable``.
    """
    try:
        from rebar.llm.errors import (
            WorkflowNotFoundError,
            WorkflowParseError,
            WorkflowUnknownStepError,
            WorkflowValidationError,
            WorkflowVersionError,
        )
    except ImportError:
        return None
    if isinstance(exc, WorkflowNotFoundError):
        return "not_found"
    if isinstance(
        exc,
        (
            WorkflowParseError,
            WorkflowValidationError,
            WorkflowVersionError,
            WorkflowUnknownStepError,
        ),
    ):
        return "invalid_input"
    return None


def _llm_error_code(exc: BaseException) -> str | None:
    """Classify an ``LLMError``-family exception (branch 7 of :func:`error_code_for`).

    The availability subtree (``LLMUnavailableError`` and its ``LLMConfigError`` /
    prompt/reviewer config subclasses) stays ``llm_unavailable``. The ``LLMRunnerError``
    subtree and every other non-availability ``LLMError`` map to the honest broad
    ``command_failed``. The two gate-admission refusals are checked FIRST and take dedicated
    codes (``gate_congested`` / ``gate_scratch_unavailable``): the gate never ran, so
    ``command_failed`` would misreport a refusal to START as a failure to complete.
    The bare ``WorkflowError`` base keeps dbca's execute-outage
    compatibility contract at ``llm_unavailable``; dedicated workflow subclasses use their
    explicit/earlier codes or fall through to ``command_failed``.
    """
    try:
        from rebar.llm.errors import (
            GateCongestedError,
            GateScratchUnavailableError,
            LLMError,
            LLMRunnerError,
            LLMUnavailableError,
            WorkflowError,
        )
    except ImportError:
        return None
    # Host congestion is not an LLM failure at all: the gate never started. It gets its own
    # code so an agent can tell "retry, the box is busy" from "the model call failed".
    if isinstance(exc, GateCongestedError):
        return "gate_congested"
    if isinstance(exc, GateScratchUnavailableError):
        return "gate_scratch_unavailable"
    if isinstance(exc, LLMRunnerError):
        return "command_failed"
    if isinstance(exc, LLMUnavailableError):
        return "llm_unavailable"
    if type(exc) is WorkflowError:
        return "llm_unavailable"
    if isinstance(exc, LLMError):
        return "command_failed"
    return None


def error_code_for(exc: BaseException) -> str:
    """Classify a rebar exception to a machine-readable error code (ticket 8a31).

    Returns a stable vocabulary code (one of :data:`KNOWN_ERROR_CODES`) derived from
    the exception's type and attributes, in precedence order:

    1. ``ConcurrencyError`` / ``ConcurrencyMismatch`` → ``concurrency_conflict``
    2. ``TicketNotFoundError`` → ``ticket_not_found``
    3. ``TrackerRootError`` → ``tracker_root_unresolved``
    4. ``InsecureUrlError`` → ``config_insecure_url``; any other ``ConfigError`` →
       ``config_unreadable``
    5. a non-empty ``exc.error_code`` attribute → that code
    6. a workflow-engine caller-input error → ``WorkflowNotFoundError`` (unknown name/run) →
       ``not_found``; ``WorkflowParseError`` / ``WorkflowValidationError`` /
       ``WorkflowVersionError`` / ``WorkflowUnknownStepError`` (a workflow that WAS found but
       will not parse/lint/migrate, or names an unknown scripted step) → ``invalid_input``.
    7. ``LLMUnavailableError`` → ``llm_unavailable``; bare ``WorkflowError`` keeps dbca's
       execute-outage compatibility at ``llm_unavailable``; other ``LLMError`` →
       ``command_failed``
    8. fallback → ``command_failed``

    Imports exception types lazily to avoid cycles (this is a stdlib-only leaf).
    """
    # 1. ConcurrencyError / ConcurrencyMismatch (before error_code branch, since
    #    ConcurrencyMismatch is a CommandError subclass with NO error_code)
    if isinstance(exc, ConcurrencyError):
        return "concurrency_conflict"
    try:
        from rebar._commands.txn import ConcurrencyMismatch

        if isinstance(exc, ConcurrencyMismatch):
            return "concurrency_conflict"
    except ImportError:
        pass

    # 2. TicketNotFoundError (a ReadError subclass, classified by type)
    try:
        from rebar._engine_support.reads import TicketNotFoundError

        if isinstance(exc, TicketNotFoundError):
            return "ticket_not_found"
    except ImportError:
        pass

    # 3. TrackerRootError
    if isinstance(exc, TrackerRootError):
        return "tracker_root_unresolved"

    # 4. ConfigError — an unreadable/malformed config is an error (operator ruling
    #    39f8-ae7c); classified by type so MCP clients can branch on the fault.
    #    InsecureUrlError is checked FIRST (ticket 7d6a): it is a deliberate
    #    cleartext-URL security-policy rejection (bug bdb8) of a config that parsed
    #    fine, so collapsing it into config_unreadable would mis-prompt an operator
    #    to "fix an unreadable config".
    try:
        from rebar._config_schema import InsecureUrlError

        if isinstance(exc, InsecureUrlError):
            return "config_insecure_url"
    except ImportError:
        pass
    try:
        from rebar._config_coercion import ConfigError

        if isinstance(exc, ConfigError):
            return "config_unreadable"
    except ImportError:
        pass

    # 5. An explicit error_code attribute (CommandError or RebarError with one set)
    code = getattr(exc, "error_code", None)
    if code:
        return code

    # 6. Workflow-engine caller-input / not-found errors — a WorkflowError subtree that must
    #    NOT collapse to llm_unavailable (bug dbca-97ac-ad96-4d6d). The bare WorkflowError base
    #    (the workflow EXECUTE base) is deliberately left to branch 7, because an execute step
    #    can genuinely fail on LLM unavailability (protects AC3 of that ticket).
    workflow_code = _workflow_caller_code(exc)
    if workflow_code is not None:
        return workflow_code

    # 7. LLMError family — availability vs. non-availability discrimination (ce6b/f75f)
    llm_code = _llm_error_code(exc)
    if llm_code is not None:
        return llm_code

    # 8. Fallback
    return "command_failed"
