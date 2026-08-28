"""RP-05 S3 — registry-driven core CLI execution.

The router selects a spelling and hands ``(name, rest)`` here; this module
resolves the single selected lazy handler from its route and invokes it through
the closed :data:`rebar._cli._registry.ADAPTER_KINDS` set, after applying the
route's :data:`rebar._cli._registry.INIT_POLICIES` init policy. It never parses
the command remainder a second time.

Stdlib + lazy imports only at module top level (like ``_registry``): command
handlers, the init middleware, and engine helpers are imported inside the
functions so importing this module pulls in no command handler or optional
dependency.
"""

from __future__ import annotations

import importlib
from typing import Protocol

from rebar._cli import _registry

_CROSS_SESSION_WARN_COMMANDS = frozenset(
    {
        "show",
        "comment",
        "edit",
        "transition",
        "reopen",
        "tag",
        "untag",
        "set-file-impact",
        "deps",
        "archive",
        "check-ac",
        "clarity-check",
        "link",
        "unlink",
    }
)


class _Handler(Protocol):
    """A resolved CLI handler: called through an adapter, returns an exit code."""

    def __call__(self, *args: object) -> int: ...


def execute(name: str, rest: list[str]) -> int:
    """Resolve and run the selected registry route's handler."""
    route = _registry.route_for(name)
    if route is None or route.handler is None:
        raise RuntimeError(f"rebar: {name!r} has no executable route")
    _apply_init(route.init, rest)
    _maybe_warn_cross_session(name, rest)
    module, _, attr = route.handler.partition(":")
    fn: _Handler = getattr(importlib.import_module(module), attr)
    return _invoke(route, name, fn, rest)


def _maybe_warn_cross_session(name: str, rest: list[str]) -> None:
    """Best-effort advisory: warn on stderr when another session holds the ticket.

    Only single-ticket commands warn; the emit never alters stdout or the exit code
    and any detector error is swallowed so the command always proceeds.
    """
    if name not in _CROSS_SESSION_WARN_COMMANDS:
        return
    try:
        token = next((arg for arg in rest if not arg.startswith("-")), None)
        if token is None:
            return
        from rebar._commands.cross_session import cross_session_warning_for

        msg = cross_session_warning_for(token, repo_root=None)
        if msg is not None:
            import sys

            sys.stderr.write("WARN: " + msg + "\n")
    except Exception:  # noqa: BLE001 — advisory warning must never break the command
        pass


def _apply_init(policy: str, rest: list[str]) -> None:
    """Apply the route's init policy through the ``rebar._cli`` module attribute.

    The init entry point is read off the ``rebar._cli`` package (where the router
    re-exports ``ensure_initialized``) at call time, so the pre-cutover behavior of
    intercepting init by patching ``rebar._cli.ensure_initialized`` is preserved.
    """
    from rebar import _cli

    if policy == "none":
        return
    if policy == "init_only":
        _cli.ensure_initialized(init_only=True)
        return
    if policy == "full":
        _cli.ensure_initialized(init_only=False)
        return
    if policy == "doctor":
        _cli.ensure_initialized(init_only="--repair" not in rest)
        return
    if policy == "fsck_recover":
        from rebar import config

        if not config.tracker_dir_override():
            _cli.ensure_initialized(init_only=False)
        return
    raise RuntimeError(f"rebar: unknown init policy {policy!r}")


def _invoke(route: _registry.Route, name: str, fn: _Handler, rest: list[str]) -> int:
    """Call ``fn`` through the route's bounded adapter kind."""
    adapter = route.adapter
    if adapter == "dispatcher":
        return fn([name, *rest])
    if adapter == "argv":
        return fn([*route.argv_prefix, *rest])
    if adapter == "argv_tracker":
        from rebar._engine_support import reads

        return fn(rest, reads.tracker_dir())
    if adapter == "argv_tracker_root":
        import os

        from rebar._engine_support import reads

        tracker = reads.tracker_dir()
        return fn(rest, tracker, os.path.dirname(tracker))
    raise RuntimeError(f"rebar: unknown adapter {adapter!r}")
