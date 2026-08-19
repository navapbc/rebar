"""Reconcile-pass project-visibility preflight (ticket a011).

Kept in a sibling module so ``__main__`` stays under the module-size cap. The
reconcile-pass entrypoint (``__main__.run_pass_result``) calls
:func:`project_visibility_preflight` STRICTLY BEFORE ``reconcile.reconcile_once``
— the only outbound-mutation call — and maps a returned :class:`PreflightAbort`
into its classified ``PassResult``. A ``None`` return lets the pass proceed.

The actual "is every mapped key + legacy_default (+ empty-mapping fallback)
visible to the bot?" decision is the reusable single source of truth in
``access_check.check_mapped_project_visibility`` (also reused by the bridge fsck
diagnostic, ticket 9702). This module only adds the reconcile-pass concerns:
which passes to gate (mutating only), the configured-backend gate, and turning a
verdict into an operator-facing abort message.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class PreflightAbort:
    """A verdict that the pass must abort before any mutation.

    ``reason`` is a stable machine tag; ``message`` is the operator-facing stderr
    line; ``details`` is threaded into the pass result's structured detail.
    """

    reason: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)


def pass_may_mutate(target_mode: Any, route: str | None) -> bool:
    """True when this pass can issue outbound mutations (so the preflight applies).

    Read-only passes are identified by the ONLY signals available before
    ``reconcile_once``: the ``preview`` route and the ``dry-run`` / ``reconcile-check``
    modes (``reconcile-check`` is already short-circuited in ``main()``; ``no_write``
    is computed INSIDE ``reconcile_once``, after this point, so it is not usable here).
    ``target_mode is None`` defaults to LIVE, which mutates.
    """
    if route == "preview":
        return False
    return getattr(target_mode, "value", None) not in {"dry-run", "reconcile-check"}


def _load_access_check() -> Any:
    # Honor a pre-seeded ``rebar_reconciler.access_check`` in sys.modules (the
    # codebase's test-seam convention, e.g. _load_sibling_keyed) before importing
    # — a plain ``from rebar_reconciler import access_check`` would prefer the
    # already-bound package attribute and bypass a seeded fake.
    cached = sys.modules.get("rebar_reconciler.access_check")
    if cached is not None:
        return cached
    from rebar_reconciler import access_check

    return access_check


def project_visibility_preflight(
    repo_root: Path,
    target_mode: Any,
    route: str | None,
    *,
    access_check: Any | None = None,
) -> PreflightAbort | None:
    """Return a :class:`PreflightAbort` if the pass must not mutate, else ``None``.

    Gated on the configured Cloud backend (``config.reconciler.backend == "jira"``);
    any other backend, or any infra error resolving the probe, logs an observable
    skip and returns ``None`` — the preflight must never itself crash a pass. The
    verdict (missing / transport-unavailable) comes from the reusable
    ``access_check.check_mapped_project_visibility``.
    """
    if not pass_may_mutate(target_mode, route):
        return None
    try:
        from rebar.config import compose_config
        from rebar_reconciler._backend_registry import select_backend

        config = compose_config()
        backend_key = getattr(config.reconciler, "backend", None)
    except Exception as exc:  # noqa: BLE001 — preflight must not crash the pass
        print(
            f"reconcile: project-visibility preflight skipped (config unavailable: {exc!r})",
            file=sys.stderr,
        )
        return None
    if backend_key != "jira":
        print(
            f"reconcile: project-visibility preflight skipped: backend {backend_key!r} "
            "has no visible-projects probe",
            file=sys.stderr,
        )
        return None
    try:
        query_project = getattr(select_backend(config), "query_project", None)
    except Exception as exc:  # noqa: BLE001 — backend init must not crash the pass
        print(
            f"reconcile: project-visibility preflight skipped (backend init failed: {exc!r})",
            file=sys.stderr,
        )
        return None

    if access_check is None:
        access_check = _load_access_check()
    try:
        result = access_check.check_mapped_project_visibility(
            repo_root, query_project=query_project
        )
    except Exception as exc:  # noqa: BLE001 — an infra error in the probe is not a verdict
        print(
            f"reconcile: project-visibility preflight skipped (probe error: {exc!r})",
            file=sys.stderr,
        )
        return None

    if result.status == "missing":
        msg = (
            "ERROR: reconcile preflight — mapped project(s) not visible to the bridge bot: "
            f"{', '.join(result.missing)}. Fix projects.json / legacy_default (or the bot's "
            "project permissions) before reconciling; no Jira write was made."
        )
        print(msg, file=sys.stderr)
        return PreflightAbort(
            "project_not_visible",
            msg,
            {"preflight": "project_not_visible", "missing": list(result.missing)},
        )
    if result.status == "transport_unavailable":
        msg = (
            "ERROR: reconcile preflight — cannot verify mapped-project visibility "
            f"({result.detail}); failing closed before any Jira write."
        )
        print(msg, file=sys.stderr)
        return PreflightAbort("transport_unavailable", msg, {"preflight": "transport_unavailable"})
    return None
