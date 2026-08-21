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


class _Handler(Protocol):
    """A resolved CLI handler: called through an adapter, returns an exit code."""

    def __call__(self, *args: object) -> int: ...


def execute(name: str, rest: list[str]) -> int:
    """Resolve and run the selected registry route's handler."""
    route = _registry.route_for(name)
    if route is None or route.handler is None:
        raise RuntimeError(f"rebar: {name!r} has no executable route")
    _apply_init(route.init, rest)
    module, _, attr = route.handler.partition(":")
    fn: _Handler = getattr(importlib.import_module(module), attr)
    return _invoke(route, name, fn, rest)


def _apply_init(policy: str, rest: list[str]) -> None:
    """Apply the route's init policy through the ``_init`` module attribute."""
    from rebar._cli import _init

    if policy == "none":
        return
    if policy == "init_only":
        _init.ensure_initialized(init_only=True)
        return
    if policy == "full":
        _init.ensure_initialized(init_only=False)
        return
    if policy == "doctor":
        _init.ensure_initialized(init_only="--repair" not in rest)
        return
    if policy == "fsck_recover":
        from rebar import config

        if not config.tracker_dir_override():
            _init.ensure_initialized(init_only=False)
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
