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
        "config_unreadable",
        "command_failed",
    }
)


def error_code_for(exc: BaseException) -> str:
    """Classify a rebar exception to a machine-readable error code (ticket 8a31).

    Returns a stable vocabulary code (one of :data:`KNOWN_ERROR_CODES`) derived from
    the exception's type and attributes, in precedence order:

    1. ``ConcurrencyError`` / ``ConcurrencyMismatch`` → ``concurrency_conflict``
    2. ``TicketNotFoundError`` → ``ticket_not_found``
    3. ``TrackerRootError`` → ``tracker_root_unresolved``
    4. ``ConfigError`` → ``config_unreadable``
    5. a non-empty ``exc.error_code`` attribute → that code
    6. ``LLMError`` → ``llm_unavailable``
    7. fallback → ``command_failed``

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

    # 6. LLMError
    try:
        from rebar.llm.errors import LLMError

        if isinstance(exc, LLMError):
            return "llm_unavailable"
    except ImportError:
        pass

    # 7. Fallback
    return "command_failed"
