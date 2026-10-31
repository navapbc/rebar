"""Intercept routes carry lazy handler strings, bounded adapter kinds, and known init policies
before _execute dispatch. Tests pin route selection after the core and bridge cutover.

Held-out oracles merged from test_cli_registry_cutover_s6_heldout.py: Held-out CLI registry tests
check adversarial route and initialization cases beyond the happy-path cutover, including command
imports, aliases, and error behavior.
"""

from __future__ import annotations

import inspect

import pytest

from rebar import _cli
from rebar._cli import _execute, _registry, main


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


# ── held-out oracles (merged from test_cli_registry_cutover_s6_heldout.py) ──


def _intercept_names() -> list[str]:
    return [r.name for r in _intercept_routes()]


# ── ADV1: every intercept family executes, none hits the None-handler RuntimeError ──


def test_every_intercept_and_audit_route_is_executable() -> None:
    """No intercept (or the ``audit`` main-intercept) may carry a ``None`` handler."""
    names = set(_intercept_names()) | {"audit"}
    for name in sorted(names):
        route = _registry.route_for(name)
        assert route is not None, name
        assert route.handler is not None, f"{name}: handler is None → execute() would RAISE"
        assert route.adapter in _registry.ADAPTER_KINDS, f"{name}: {route.adapter!r}"
        assert route.init in _registry.INIT_POLICIES, f"{name}: {route.init!r}"


@pytest.mark.parametrize("name", sorted(set(_intercept_names()) | {"audit"}))
def test_execute_resolves_and_invokes_each_intercept_handler(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_execute.execute`` resolves each intercept's handler and calls it — no RuntimeError.

    The handler is stubbed at its own dotted target so no command logic runs; the point is
    that the executor finds a real callable (never ``handler is None``) and hands it the bare
    remainder through the ``argv`` adapter shape.
    """
    route = _registry.route_for(name)
    assert route is not None and route.handler is not None
    monkeypatch.setattr("rebar._cli.ensure_initialized", lambda **_k: None)

    seen: list[object] = []
    module, _, attr = route.handler.partition(":")
    monkeypatch.setattr(f"{module}.{attr}", lambda *a, **k: seen.append((a, k)) or 0)

    rc = _execute.execute(name, ["ZZ-token"])
    assert rc == 0, f"{name}: execute returned {rc!r}"
    assert seen, f"{name}: handler was never invoked"
    # argv-adapter shape: the handler receives the bare remainder as its first positional.
    (args, _kwargs) = seen[0]
    assert args and args[0] == ["ZZ-token"], f"{name}: adapter passed {args!r}"


# ── the intercept ladder is GONE: no second selection authority survives ──


def test_every_intercept_spelling_reaches_the_executor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Observable single-authority proof: EVERY intercept spelling routes through the executor.

    If any bespoke ``if argv[0] == …`` arm still shadowed the registry, that spelling would
    return without ``_execute.execute`` ever being called.
    """
    monkeypatch.setattr("rebar._cli.ensure_store_mounted_best_effort", lambda: None)
    for name in sorted(set(_intercept_names()) | {"audit"}):
        seen: list[tuple[str, list[str]]] = []
        monkeypatch.setattr(
            "rebar._cli._execute.execute",
            lambda n, rest, _seen=seen: _seen.append((n, rest)) or 0,
        )
        rc = main([name, "ZZ-token"])
        assert rc == 0, f"{name}: router returned {rc!r} without delegating"
        assert seen == [(name, ["ZZ-token"])], f"{name}: did not reach the executor ({seen!r})"


def test_router_body_carries_no_explicit_intercept_ladder() -> None:
    """Source guard accompanying the observable proof: the ``if argv[0] == "<intercept>"``
    ladder is deleted from the router body — the registry is the sole selection authority."""
    src = inspect.getsource(_cli._main_dispatch)
    offenders = [
        name
        for name in _intercept_names()
        if f'argv[0] == "{name}"' in src or f"argv[0] == '{name}'" in src
    ]
    assert offenders == [], f"router still hard-codes an intercept arm for: {offenders}"


# ── the duplicate policy frozensets are retired — one authority, the registry ──

_DEAD_LITERAL_SETS = (
    "_READS_INIT_ONLY",
    "_READS_NO_INIT",
    "_FIELD_READS",
    "_LOOKUPS",
    "_DESCENDANTS",
    "_GATES",
    "_SIGNING",
    "_LIFECYCLE",
    "_COMPACT",
    "_BRIDGE",
    "_IO",
    "_WRITES_FULL",
    "_HIDDEN_ALIASES",
)

# The only policy sets the router still binds — derived from the registry, not literal.
_LIVE_ROUTER_SETS = ("_INTERCEPTS", "_NO_AUTO_MOUNT", "_LEGACY_OUTPUT", "_CONFIRM_SCOPE")


def test_dead_literal_policy_frozensets_are_gone() -> None:
    """The migration-only duplicate policy censuses no longer exist as ``_cli`` attributes."""
    still_present = [name for name in _DEAD_LITERAL_SETS if hasattr(_cli, name)]
    assert still_present == [], f"duplicate policy literals still shipped: {still_present}"


def test_live_router_sets_are_the_registry_derived_sets() -> None:
    """The sets the router still uses ARE the registry's derived sets (single source)."""
    derived = _registry.derive_policy_sets()
    for name in _LIVE_ROUTER_SETS:
        assert getattr(_cli, name) == derived[name], name


def test_confirm_scope_still_equals_writes_plus_lifecycle() -> None:
    """``_CONFIRM_SCOPE`` (the mutating-verb confirmation channel) is preserved by value."""
    derived = _registry.derive_policy_sets()
    assert _cli._CONFIRM_SCOPE == (derived["_WRITES_FULL"] | derived["_LIFECYCLE"])


# ── retired / unknown spellings stay unknown ──


def test_retired_bridge_verb_stays_unknown() -> None:
    assert _registry.route_for("purge-bridge") is None
    assert main(["purge-bridge"]) == 1


def test_unknown_spelling_stays_unknown() -> None:
    assert _registry.route_for("definitely-not-a-command") is None
    assert main(["definitely-not-a-command"]) == 1


# ── identity's conditional-init contract survives the cutover ──


def test_identity_inits_full_for_a_real_invocation(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[bool] = []
    monkeypatch.setattr(
        "rebar._cli.ensure_initialized", lambda *, init_only: calls.append(init_only)
    )
    monkeypatch.setattr("rebar._commands.identity.identity_cli", lambda argv, **k: 0)
    _execute.execute("identity", ["create", "--email", "x@y.z"])
    assert calls == [False], "identity must full-init (reconverge) before a real invocation"


def test_identity_skips_init_for_help(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[bool] = []
    monkeypatch.setattr(
        "rebar._cli.ensure_initialized", lambda *, init_only: calls.append(init_only)
    )
    monkeypatch.setattr("rebar._commands.identity.identity_cli", lambda argv, **k: 0)
    _execute.execute("identity", ["--help"])
    assert calls == [], "identity --help must NOT initialize the store"
