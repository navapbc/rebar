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


class ConcurrencyError(RebarError):
    """Optimistic-concurrency rejection (the ticket changed since it was read).

    Raised by :func:`rebar.transition` when the engine reports exit code 10.
    """
