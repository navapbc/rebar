"""Full-extra lazy-reference contract for the CLI route registry (RP-05 S5, ticket 6755).

The registry stores every handler / parser-factory as a LAZY ``"module:attr"`` string that is
never imported at registry construction (the import-isolation contract). This contract closes
the loop the other way: in an environment with the runtime extras available, EVERY lazy
reference must actually RESOLVE (import the module, fetch the attribute) — a typo in a dotted
path or a renamed factory is a latent dispatch failure that no import-isolation test can see.
It also validates every capability-to-extra mapping against the declared packaging extras.

Resolution imports the referenced modules but NEVER calls a handler — no command logic runs,
no store is touched. (Parser factories are cheap and side-effect-free, so they are invoked to
prove they build a real ``ArgumentParser``.)
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
import tomllib

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]


def _resolve(ref: str):
    module_name, _, attr = ref.partition(":")
    module = importlib.import_module(module_name)
    return getattr(module, attr)


def _live_routes():
    from rebar._cli._registry import ROUTES

    return [r for r in ROUTES if not r.retired]


def test_every_handler_reference_resolves():
    """Every non-retired route's handler ``module:attr`` imports and yields a callable."""
    failures: list[str] = []
    for route in _live_routes():
        if route.handler is None:
            continue
        try:
            target = _resolve(route.handler)
        except Exception as exc:  # noqa: BLE001 - report which ref, not just the first raise
            failures.append(f"{route.name}: handler {route.handler!r} -> {exc!r}")
            continue
        if not callable(target):
            failures.append(f"{route.name}: handler {route.handler!r} is not callable")
    assert not failures, "unresolved handler references:\n" + "\n".join(failures)


def test_every_parser_factory_reference_resolves_and_builds():
    """Every non-retired route's parser factory imports and builds an ArgumentParser."""
    import argparse

    failures: list[str] = []
    for route in _live_routes():
        if route.parser_factory is None:
            continue
        try:
            factory = _resolve(route.parser_factory)
            parser = factory(prog=f"rebar {route.name}")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{route.name}: parser {route.parser_factory!r} -> {exc!r}")
            continue
        if not isinstance(parser, argparse.ArgumentParser):
            failures.append(f"{route.name}: parser factory did not return an ArgumentParser")
    assert not failures, "unresolved/invalid parser factories:\n" + "\n".join(failures)


def test_every_route_capability_is_a_known_capability():
    """Every capability a route advertises is a key of the descriptive capability registry."""
    from rebar._capabilities import CAPABILITY_KEYS

    failures: list[str] = []
    for route in _live_routes():
        for cap in route.capabilities:
            if cap not in CAPABILITY_KEYS:
                failures.append(f"{route.name}: unknown capability {cap!r}")
    assert not failures, "\n".join(failures)


def test_registry_validate_reports_no_findings():
    """The registry's own structural validation (references, capabilities, adapters, init
    policies) passes for the shipped route table — the S3/S4 execution metadata resolves."""
    from rebar._cli._registry import ROUTES, validate

    assert validate(ROUTES) == ()


def test_every_capability_extra_is_declared():
    """Each capability's packaging extra is in the descriptive DECLARED_EXTRAS set, and the
    capability registry validates clean (capability-to-extra references are consistent)."""
    from rebar import _capabilities

    assert _capabilities.validate() == ()
    for cap in _capabilities.CAPABILITIES:
        assert cap.extra in _capabilities.DECLARED_EXTRAS, (
            f"capability {cap.key!r} maps to undeclared extra {cap.extra!r}"
        )


def test_declared_extras_match_pyproject_optional_dependencies():
    """The single-sourced DECLARED_EXTRAS mirror pyproject's runtime extras (minus ``dev``) —
    a capability can never point at a packaging extra the wheel does not actually ship."""
    from rebar._capabilities import DECLARED_EXTRAS

    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    optional = set(data["project"]["optional-dependencies"])
    runtime_extras = optional - {"dev"}
    assert DECLARED_EXTRAS == frozenset(runtime_extras), (
        f"DECLARED_EXTRAS {sorted(DECLARED_EXTRAS)} != pyproject runtime extras "
        f"{sorted(runtime_extras)}"
    )
