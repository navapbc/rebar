"""RP-05 S6 HELD-OUT oracle — adversarial cutover proofs (unit tier).

Withheld from the implementation subagent; restored and validated by the orchestrator after
the happy-path implementation lands. Covers the cases that separate a real cutover from one
that only satisfies the happy path:

* every intercept family dispatches through the executor with the bare-remainder adapter
  shape and NEVER trips the ``handler is None`` RuntimeError (ADV1);
* the explicit intercept ladder is gone — no second selection authority survives, proven
  both observably (every intercept spelling reaches ``_execute.execute``) and by a source
  guard on the router body;
* the duplicate policy frozensets are retired — the router's live sets are the registry's
  derived sets, and the dead literal exports no longer exist as ``_cli`` attributes;
* retired / unknown spellings stay unknown; and
* ``identity``'s conditional-init contract survives the cutover (full init unless help).
"""

from __future__ import annotations

import inspect

import pytest

from rebar import _cli
from rebar._cli import _execute, _registry, main


def _intercept_routes() -> tuple[_registry.Route, ...]:
    return tuple(r for r in _registry.ROUTES if r.intercept)


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
