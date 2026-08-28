"""Advisory ``CrossSessionWarning`` emission for single-ticket library functions.

Single-ticket public library entry points call :func:`emit_cross_session_warning`
before mutating, so a caller acting from a session other than the one holding the
ticket's live claim gets a best-effort heads-up on the stdlib ``warnings`` channel.
The warning is purely advisory: it never alters behavior and never raises.
"""

from __future__ import annotations

import contextlib
import contextvars
import warnings
from collections.abc import Iterator

_EMITTING: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "_rebar_cross_session_emitting", default=False
)


class CrossSessionWarning(UserWarning):
    """Emitted when a single-ticket call acts on a ticket held by another session."""


@contextlib.contextmanager
def suppress_cross_session_warning() -> Iterator[None]:
    """Suppress library ``CrossSessionWarning`` emission within the context.

    An outer surface that already advises about cross-session access (for example
    the CLI, which writes its own ``WARN:`` line before dispatching to the library)
    holds this guard across the underlying call so the operation warns exactly once.
    """
    token = _EMITTING.set(True)
    try:
        yield
    finally:
        _EMITTING.reset(token)


def emit_cross_session_warning(
    ticket_id: str, *, repo_root: str | None = None, stacklevel: int = 3
) -> None:
    """Best-effort advisory warning when another session holds ``ticket_id``'s claim.

    Never raises and never alters the caller's result; guarded against re-entrancy
    because the detector itself reads the ticket through the public library surface.
    """
    if _EMITTING.get():
        return
    token = _EMITTING.set(True)
    try:
        try:
            from rebar._commands.cross_session import cross_session_warning_for

            msg = cross_session_warning_for(ticket_id, repo_root=repo_root)
            if msg is not None:
                warnings.warn(msg, CrossSessionWarning, stacklevel=stacklevel)
        except Exception:  # noqa: BLE001 - advisory warning must never break the call
            pass
    finally:
        _EMITTING.reset(token)
