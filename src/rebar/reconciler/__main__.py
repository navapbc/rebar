#!/usr/bin/env python3
"""rebar.reconciler.__main__ — the ``python -m rebar.reconciler`` command line.

Invoked as ``python -m rebar.reconciler`` (``rebar bridge`` / ``rebar reconcile`` launch it).
It parses argv, binds the operation snapshot, runs one pass through
``guarded_pass.run_guarded_pass`` (the guard sequence the library and MCP share), and
renders the classified result for the invoking route.

Exit codes: the canonical ``preview``/``sync`` routes use 0/1/2; the compatibility
route keeps its historical 3/4/75 sentinels (``guarded_pass._Disposition``).
"""

from __future__ import annotations

import json
import sys

from rebar.reconciler import guarded_pass
from rebar.reconciler.guarded_pass import _MODE_KEY, _REQUEST_KEY, PassResult, _Disposition


def _finish_disposition(
    disposition: _Disposition,
    route: str | None,
    *,
    legacy_message: str | None = None,
    canonical_message: str | None = None,
) -> int:
    """Render one classified result through the canonical or compatibility adapter."""
    if route in {"preview", "sync"}:
        if disposition.canonical_exit == 0:
            print(canonical_message or f"BRIDGE_STATE: {disposition.state}", file=sys.stderr)  # noqa: T201
        elif canonical_message is not None:
            print(canonical_message, file=sys.stderr)  # noqa: T201
        elif legacy_message is not None:
            print(legacy_message, file=sys.stderr)  # noqa: T201
        return disposition.canonical_exit
    if legacy_message is not None:
        print(legacy_message, file=sys.stderr)  # noqa: T201
    return disposition.legacy_exit


def _render_pass(result: PassResult, route: str | None) -> int:
    """Print one classified pass for *route* and return its process exit code.

    Canonical preview/sync use 0/1/2 while the compatibility route preserves the
    historical 3/4/75 sentinels.
    """
    details = result.details
    if details.get("no_write"):
        if route not in {"preview", "sync"}:
            print(  # noqa: T201
                f"OK: dry-run computed {details.get('mutation_count', 0)} mutations "
                "(0 applied, no writes)",
                file=sys.stderr,
            )
        print(json.dumps(details))  # noqa: T201
    elif route not in {"preview", "sync"} and details and result.legacy_message is None:
        computed = details.get("mutation_count", 0)
        applied = details.get("mutations_applied", computed)
        failures = details.get("mutation_failures", 0)
        if computed == 0 and applied == 0:
            print("OK: steady-state pass converged — 0 mutations")  # noqa: T201
        elif failures == 0:
            print(f"OK: applied {applied} of {computed} mutations")  # noqa: T201
        else:
            print(f"OK: applied {applied} of {computed} mutations ({failures} failed)")  # noqa: T201
    return _finish_disposition(
        result.disposition,
        route,
        legacy_message=result.legacy_message,
        canonical_message=result.canonical_message,
    )


def _dry_run_enumeration_exit(request) -> int | None:
    """Handle the compatibility directory-enumeration route before lock checks."""
    if not request.dry_run_enumerate:
        return None
    repo_root = request.repo_root
    from rebar.config import reconciler_repo_root as _owned_repo_root
    from rebar.config import tracker_dir as _resolve_store

    resolved_root = repo_root if repo_root is not None else _owned_repo_root()
    # RESOLVED, not composed: enumerating a hardcoded repo-root directory would list
    # nothing (or the wrong tree) on a deployment whose store has been relocated.
    tickets_dir = _resolve_store(resolved_root)
    if not tickets_dir.is_dir():
        return 0
    for entry in sorted(tickets_dir.iterdir()):
        if not entry.is_dir():
            continue
        if ".scratch" in entry.parts:
            continue
        print(entry)  # noqa: T201
    return 0


def _bind_operation_snapshot(repo_root):
    """Compose and bind one ``OperationSnapshot`` for the remainder of ``main``.

    Return the entered context manager so the caller retains it through the pass and
    exits it explicitly. Retention prevents premature generator cleanup. Explicit exit
    prevents context leakage between in-process calls. Configuration errors preserve
    fail-open ambient resolution.
    """
    from rebar._operation_config import compose_and_bind_operation_snapshot

    cm = compose_and_bind_operation_snapshot(repo_root=repo_root)
    cm.__enter__()
    return cm


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m rebar.reconciler``.

    Parse --mode (validated BEFORE any fetcher reference) and --repo-root, bind the
    operation snapshot, then hand the request to ``guarded_pass.run_guarded_pass``
    and render its result.
    """
    # Observability floor: install a stderr handler on the ``rebar`` logger root,
    # which the reconciler's ``rebar.reconciler.*`` loggers sit under.
    from rebar._logging import install_stderr_handler

    install_stderr_handler("rebar")

    mode_mod = guarded_pass._load_sibling_keyed(_MODE_KEY, "mode.py")
    request_mod = guarded_pass._load_sibling_keyed(_REQUEST_KEY, "request.py")
    try:
        request = request_mod.normalize_request(argv, mode_mod)
    except (request_mod.RequestError, ValueError) as exc:
        print(str(exc), file=sys.stderr)  # noqa: T201
        return 2
    route = getattr(request, "route", None)

    # Bind one composed operation snapshot through the pass and release it after.
    operation_snapshot_cm = _bind_operation_snapshot(request.repo_root)
    try:
        # This compatibility path remains before mode and advisory-lock checks.
        enumeration_exit = _dry_run_enumeration_exit(request)
        if enumeration_exit is not None:
            return enumeration_exit
        return _render_pass(guarded_pass.run_guarded_pass(request), route)
    finally:
        operation_snapshot_cm.__exit__(None, None, None)


if __name__ == "__main__":
    sys.exit(main())
