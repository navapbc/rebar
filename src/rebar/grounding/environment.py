"""Environment-aware symbol resolution for the T1 refutation lane (bug 406f).

A reviewer's repo-scoped file tools (and the ctags repo-wide index the T1 lane in
:mod:`.resolve` builds) cannot see a THIRD-PARTY dependency that lives in
``site-packages``, so a symbol imported from an installed library reads as "not
found in the repo" and gets wrongly asserted non-existent. This module closes that
gap DETERMINISTICALLY by consulting the SAME Python environment the code runs
against: :func:`importlib.util.find_spec` proves a module exists (without executing
it), and an import + ``getattr`` proves a ``module.attr`` member exists.

Confirm-only, exactly like the rest of the lane: :func:`refute_via_environment` can
only UPGRADE a not-found ``abstain`` to a ``refuted``; an unresolvable name still
abstains (never a false absence). Extracted from :mod:`.resolve` (which re-exports
:func:`resolve_in_environment` and :data:`BACKEND_ENV`) so the resolver stays under
the module-size cap; the evidence bridge borrows :mod:`.resolve`'s schema-safe
reference helper through a deferred import to keep the two modules acyclic.
"""

from __future__ import annotations

import importlib
import importlib.util
import re
from collections.abc import Mapping
from typing import Any

from . import evidence as ev

#: The installed-environment (importlib) refute backend for symbol/import/member
#: references — resolves a third-party/stdlib name the repo-scoped index can't see.
BACKEND_ENV = "environment"

#: A syntactically valid (possibly dotted) Python import path. Guards importlib
#: against being handed an arbitrary string as a module name.
_IMPORTABLE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")


def resolve_in_environment(
    name: str, *, container: str | None = None, language: str | None = None
) -> dict[str, Any] | None:
    """Resolve a Python reference against the installed environment; ``None`` if not.

    Returns a location dict — ``{"module": …, "origin": …}`` for a module, or
    ``{"module": …, "attr": …, "origin": …}`` for a bound member — when the
    reference is importable, else ``None``. Tries the most specific interpretation
    first: an explicit ``container`` (``from container import name``), then a dotted
    ``name`` split into ``module.attr`` (and ``name`` itself as a submodule path),
    then a bare ``name`` as a top-level module.

    Python-only (a declared non-Python language returns ``None``). Bounded side
    effects: ``find_spec`` never executes the target module; an attribute bind
    imports the module (running its package ``__init__``) but only when an attribute
    is actually requested. Every failure is swallowed to ``None`` — it NEVER raises
    and NEVER reports a false resolution.
    """
    if language is not None and language.strip().lower() not in ("", "python"):
        return None
    nm = (name or "").strip()
    if not nm:
        return None
    candidates: list[tuple[str, str | None]] = []
    ctr = (container or "").strip()
    if ctr:
        candidates.append((ctr, nm))
    if "." in nm:
        head, _, leaf = nm.rpartition(".")
        if head and leaf:
            candidates.append((head, leaf))
        candidates.append((nm, None))  # nm may itself be a dotted submodule path
    else:
        candidates.append((nm, None))  # a bare top-level module name
    for mod, attr in candidates:
        origin = _module_origin(mod)
        if origin is None:
            continue
        if attr is None:
            return {"module": mod, "origin": origin}
        if _attribute_exists(mod, attr):
            return {"module": mod, "attr": attr, "origin": origin}
    return None


def _module_origin(module: str) -> str | None:
    """Return ``module``'s spec origin if importable in the environment, else ``None``.

    Uses :func:`importlib.util.find_spec`, which LOCATES (does not execute) the
    target module. Fail-closed: any resolution error → ``None`` (a name we cannot
    import is simply "not confirmed", never a claimed absence)."""
    if not _IMPORTABLE_NAME_RE.match(module):
        return None
    try:
        spec = importlib.util.find_spec(module)
    except Exception:  # noqa: BLE001 — find_spec imports parent packages and can raise anything; a failure is just "unresolved"
        return None
    if spec is None:
        return None
    return spec.origin or "namespace"


def _attribute_exists(module: str, attr: str) -> bool:
    """True iff ``module.attr`` binds after importing ``module`` (fail-closed to False)."""
    if not _IMPORTABLE_NAME_RE.match(attr):
        return False
    try:
        mod = importlib.import_module(module)
    except Exception:  # noqa: BLE001 — a third-party module can raise anything at import; a failed import is "unresolved", never a raise
        return False
    return hasattr(mod, attr)


def refute_via_environment(ref: Mapping[str, Any]) -> dict[str, Any] | None:
    """Build a ``refuted`` evidence record iff ``ref`` resolves in the environment.

    The bridge between :func:`resolve_in_environment` and the evidence contract: on
    a hit it emits a ``refuted`` record at ``TIER_T1`` with the ``environment``
    backend (the external origin is carried in ``detail``, not ``location`` — a
    site-packages path is not a repo-relative def-site); on a miss it returns
    ``None`` so the caller keeps its confirm-only abstain."""
    # Deferred import: the schema-safety helper lives in `.resolve`, which imports
    # this module at load — importing it here at call time keeps the pair acyclic.
    from .resolve import _schema_safe_reference

    loc = resolve_in_environment(
        str(ref.get("name", "")),
        container=ref.get("container") if isinstance(ref.get("container"), str) else None,
        language=ref.get("language") if isinstance(ref.get("language"), str) else None,
    )
    if loc is None:
        return None
    qualified = loc["module"] + (f".{loc['attr']}" if loc.get("attr") else "")
    cov = ev.coverage(backend=BACKEND_ENV, status=ev.STATUS_RAN)
    return ev.refuted(
        provenance_tier=ev.TIER_T1,
        coverage=cov,
        reference=_schema_safe_reference(ref),
        detail=f"{qualified!r} is importable from the installed environment "
        f"(origin={loc.get('origin')}) — a third-party/stdlib symbol the repo index "
        "cannot see; asserted absence disproved",
    )


__all__ = ["BACKEND_ENV", "resolve_in_environment", "refute_via_environment"]
