"""Intercept routes carry lazy handler strings, bounded adapter kinds, and known init policies
before _execute dispatch. Tests pin route selection after the core and bridge cutover."""

from __future__ import annotations

import pytest

from rebar._cli import _registry, main


def _intercept_routes() -> tuple[_registry.Route, ...]:
    return tuple(r for r in _registry.ROUTES if r.intercept)


def test_every_intercept_route_carries_executable_metadata() -> None:
    """ADV1: no intercept route may reach ``_execute.execute`` with ``handler is None``.

    Every intercept route must name a lazy handler string, a bounded adapter kind, and a
    known init policy — the exact execution metadata the executor needs to invoke it.
    """
    routes = _intercept_routes()
    assert routes, "no intercept routes found — the registry census is empty"
    for route in routes:
        assert route.handler is not None, f"{route.name}: intercept route carries no handler"
        assert isinstance(route.handler, str), f"{route.name}: handler must stay a lazy string"
        assert route.adapter in _registry.ADAPTER_KINDS, (
            f"{route.name}: adapter {route.adapter!r} not in the closed adapter set"
        )
        assert route.init in _registry.INIT_POLICIES, (
            f"{route.name}: init policy {route.init!r} not in the closed set"
        )


def test_router_routes_an_intercept_through_the_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cutover itself: an intercept spelling routes through ``_execute.execute``.

    The router hands the executor ``(name, rest)`` with the remainder verbatim — no
    bespoke ``if argv[0] == "review-plan"`` arm sits in front of it any more.
    """
    seen: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        "rebar._cli._execute.execute", lambda name, rest: seen.append((name, rest)) or 0
    )
    monkeypatch.setattr("rebar._cli.ensure_store_mounted_best_effort", lambda: None)

    rc = main(["review-plan", "--force", "abcd-1234"])

    assert rc == 0
    assert seen == [("review-plan", ["--force", "abcd-1234"])], (
        "an intercept spelling did not delegate to the executor"
    )


def test_intercept_executes_end_to_end_in_process(capsys: pytest.CaptureFixture[str]) -> None:
    """A no-store intercept (``explain``) executes through the registry and prints its guide.

    ``explain`` needs no store, so it exercises the full registry dispatch path without a
    fixture. A working cutover prints the guide and returns zero. A route without a handler
    would raise instead.
    """
    rc = main(["explain", "plan"])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.strip(), "explain produced no output — the intercept did not execute"
