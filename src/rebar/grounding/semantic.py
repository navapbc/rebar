"""The T2 semantic-resolution dispatch seam (epic 850f).

Fills the **abstain-by-default T2 seam** the v1 floor shipped (epic 8f6c): member/
dotted references, and not-found bare symbols/imports, abstain at T1 today; this
module lets an opt-in semantic backend try to DISPROVE such an asserted absence and
emit a trustworthy ``refuted`` at :data:`~rebar.grounding.evidence.TIER_T2`.

Per ADR 0030 this is a **dispatch function, not a plugin registry** (Rule-of-Three):
one concrete backend ships (pyright, story S3). A future backend adds its name to
:data:`T2_BACKENDS` and a branch to :func:`_resolve_backend` — the ``Protocol`` +
registry are introduced only when a third real call-site justifies them.

Contract (confirm-only, fail-open):

* :func:`refute_semantic` returns a normalized ``refuted``/``abstain`` evidence
  record at ``TIER_T2``, **or ``None``** meaning "T2 declined to run (not enabled,
  no backend selected/available) — keep the caller's T1 record". With T2 disabled
  (the default) it returns ``None``, so the oracle is byte-identical to the pre-epic
  floor.
* A backend is selected by config (``t2_enabled`` + ``t2_backend``); an unknown/
  absent selection resolves to ``None`` (never a raise).

stdlib-only + import-clean: importing this module pulls no heavy stack. The concrete
backend module (which may shell out to a checker) is imported **lazily** inside the
dispatch, only when that backend is actually selected — a non-adopting client pays
nothing.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from . import evidence as ev

#: The CLOSED set of selectable T2 backend names (advertised by
#: :func:`available_backends` and used to validate ``t2_backend``). A new backend
#: adds its name here plus a branch in :func:`_resolve_backend` / :func:`_backend_version`.
T2_BACKENDS: tuple[str, ...] = ("pyright",)

#: A backend callable: ``refute(reference, *, repo_root, timeout, cache) -> dict``
#: returning ONE (un-normalized) evidence record. Matches the shape of the T1 lane's
#: ``refute_absence`` so a backend needs no bespoke protocol object.
BackendRefute = Callable[..., dict[str, Any]]


def _resolve_backend(name: str) -> BackendRefute | None:
    """Map a backend ``name`` to its ``refute`` callable, or ``None`` if unknown.

    The single dispatch seam. A new backend adds one branch here (and its name to
    :data:`T2_BACKENDS`). The concrete module is imported lazily so this module
    stays import-clean for non-adopting clients. Returns ``None`` for any name not
    wired — the fail-open "no such backend" path.
    """
    if name == "pyright":
        from . import pyright_backend  # lazy — keeps this module import-clean

        return pyright_backend.refute
    return None


def refute_semantic(
    reference: Mapping[str, Any],
    *,
    repo_root: str,
    config: Any,
    timeout: float | None = None,
    cache: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Try to refute an asserted-absent ``reference`` via the opt-in T2 backend.

    Returns a normalized ``TIER_T2`` evidence record (``refuted``/``abstain``), or
    ``None`` when T2 declined to run (disabled / no backend selected / unknown
    backend). NEVER raises on a backend failure — a backend that itself fails open
    returns an ``abstain`` record; anything else here resolves to ``None`` so the
    caller keeps its T1 record.

    ``config`` is a :class:`~rebar.grounding.resolve.GroundingConfig` (read for
    ``t2_enabled`` / ``t2_backend`` / ``t2_timeout_seconds``). ``cache`` is a
    caller-owned mapping a backend may use to reuse one checker invocation across
    many references in a single review (no module-global state → no cross-thread
    concern).
    """
    if not getattr(config, "t2_enabled", False):
        return None
    name = getattr(config, "t2_backend", None)
    if not isinstance(name, str) or not name:
        return None
    run = _resolve_backend(name)
    if run is None:
        return None
    eff_timeout = (
        timeout if timeout is not None else float(getattr(config, "t2_timeout_seconds", 30.0))
    )
    rec = run(reference, repo_root=repo_root, timeout=eff_timeout, cache=cache)
    return ev.normalize_evidence(rec) if rec is not None else None


def _backend_version(name: str) -> str | None:
    """Best-effort version probe for a backend ``name`` (fail-open to ``None``)."""
    if name == "pyright":
        from . import pyright_backend  # lazy — keeps this module import-clean

        return pyright_backend.version()
    return None


def available_backends() -> list[dict[str, Any]]:
    """One ``{name, available, version}`` entry per name in :data:`T2_BACKENDS`.

    Fed to ``oracle._backend_availability`` so ``grounding_info`` reports the T2
    tier. Each probe is best-effort (an absent tool reports ``available=False`` with
    a null version, never a raise). Empty while :data:`T2_BACKENDS` is empty.
    """
    out: list[dict[str, Any]] = []
    for name in T2_BACKENDS:
        try:
            version = _backend_version(name)
        except Exception:  # noqa: BLE001 — a contract probe must never fail the read tool
            version = None
        out.append({"name": name, "available": version is not None, "version": version})
    return out


def is_t2_territory(reference: Mapping[str, Any]) -> bool:
    """Whether a reference is one the T2 lane may try to resolve.

    T2 territory is member/dotted references and bare ``symbol``/``import`` names
    (the kinds the T1 lane abstains on for semantic reasons). ``file`` (a path
    existence check) and ``dependency`` (the T0 registry lane) are NOT escalated.
    """
    kind = reference.get("kind")
    if kind in ("symbol", "import", "member"):
        return True
    name = reference.get("name")
    # A dotted name on any non-file/dependency kind is member territory.
    return isinstance(name, str) and "." in name and kind not in ("file", "dependency")


__all__ = [
    "T2_BACKENDS",
    "refute_semantic",
    "available_backends",
    "is_t2_territory",
]
