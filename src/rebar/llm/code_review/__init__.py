"""Code-review capability package (epic b744).

Holds the four-pass code-review gate's building blocks — the diff context-assembler
(:mod:`assemble`), the overlay-id registry + filters (:mod:`registry`), and the
structured-output contract (:mod:`contracts`). The historical SINGLE-PASS reviewer lives in
:mod:`single_pass` (the route WS4 retires); its public API (``review_code`` /
``select_code_reviewers``) is LAZILY re-exported from this package for backward
compatibility, so importing the new modules (``assemble`` / ``registry``) does NOT pull the
single-pass route in at package-import time.

Importing this package registers the code-review structured-output contract (so the live
runner can emit ``recommend_overlays``); the import is cheap (no pydantic until a model is
built).
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

# Register the structured-output contract on package import (cheap — pydantic is lazy).
from rebar.llm.code_review import contracts as _contracts  # noqa: F401

if TYPE_CHECKING:
    # Static re-export FOR TYPE-CHECKERS ONLY: the single-pass public API is provided at
    # RUNTIME via __getattr__ below (lazy), but mypy needs the concrete types here so callers
    # (e.g. ``rebar.llm.review_code``) stay typed rather than ``Any``. This block is never
    # executed at runtime, so it does NOT eagerly import the single-pass route.
    from rebar.llm.code_review.single_pass import (  # noqa: F401
        review_code,
        select_code_reviewers,
    )

_SUBMODULES = ("contracts", "assemble", "registry", "single_pass")


def __getattr__(name: str) -> Any:
    """Lazily re-export the single-pass public API (``review_code`` / ``select_code_reviewers``
    and its helpers) from :mod:`single_pass`, so legacy ``from rebar.llm.code_review import …``
    callers keep working WITHOUT this package eagerly importing the single-pass route. WS4
    retires :mod:`single_pass`; the new ``assemble`` / ``registry`` modules never touch it.

    Uses ``importlib.import_module`` (NOT ``from . import``) so resolving a submodule name does
    not re-enter this hook via the fromlist ``hasattr`` check (which would recurse)."""
    if name in _SUBMODULES:
        return importlib.import_module(f"{__name__}.{name}")
    single_pass = importlib.import_module(f"{__name__}.single_pass")
    try:
        return getattr(single_pass, name)
    except AttributeError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
