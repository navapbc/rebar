"""The ``_registry`` / ``_registry_checks`` split is behaviour-preserving and stays wired.

Extracting the validation cluster out of ``_registry.py`` (ticket ``bc66-4827-355a-43bd``)
is pure code motion, and pure code motion has two silent failure modes that an import-only
smoke test would miss: a re-export the consumers need could be dropped, and a check could
be moved but left out of ``_CHECKS`` — the registry would then validate CLEAN because the
rule stopped running, not because the tree is good.

So this pins the consumed surface, asserts every defined check is actually wired, and
exercises each check independently through the public ``validate`` against a route table
crafted to violate exactly that rule.
"""

from __future__ import annotations

import inspect

import pytest

from rebar._cli import _registry, _registry_checks
from rebar._cli._registry import ROUTES, Route, validate


def test_public_surface_resolves() -> None:
    """AC2: every externally-consumed name still resolves on ``_registry``."""
    missing = [name for name in _registry.__all__ if not hasattr(_registry, name)]
    assert not missing, f"the split dropped re-exports consumers depend on: {missing}"


def test_declared_surface_covers_the_split_names() -> None:
    """The four names that physically moved must be among the pinned re-exports."""
    moved = {"ADAPTER_KINDS", "INIT_POLICIES", "KNOWN_CAPABILITIES", "Finding"}
    assert moved <= set(_registry.__all__)


def test_every_defined_check_is_wired_into_the_check_tuple() -> None:
    """A check that moved but was left out of ``_CHECKS`` would silently stop running."""
    defined = {
        name
        for name, obj in vars(_registry_checks).items()
        if name.startswith("_check_") and inspect.isfunction(obj)
    }
    wired = {check.__name__ for check in _registry_checks._CHECKS}
    assert defined == wired, f"defined but not wired: {sorted(defined - wired)}"


def test_committed_registry_validates_clean() -> None:
    """AC3: the real table still produces no findings after the move."""
    assert validate(ROUTES) == ()


def _codes(routes: tuple[Route, ...]) -> set[str]:
    return {finding.code for finding in validate(routes)}


@pytest.mark.parametrize(
    ("code", "routes"),
    [
        (
            "duplicate",
            (Route(name="dup", group="read"), Route(name="dup", group="read")),
        ),
        (
            "alias_retired_collision",
            (
                Route(name="gone", group="read", retired=True),
                Route(name="live", group="read", aliases=("gone",)),
            ),
        ),
        (
            "unknown_capability",
            (Route(name="c", group="read", capabilities=("not-a-real-capability",)),),
        ),
        (
            "malformed_reference",
            (Route(name="r", group="read", handler="no-colon-here", adapter="argv"),),
        ),
        (
            "contradiction",
            (Route(name="x", group="read", retired=True, hidden=True),),
        ),
        (
            "unknown_adapter",
            (Route(name="a", group="read", adapter="teleport"),),
        ),
        (
            "unknown_init",
            (Route(name="i", group="read", init="whenever"),),
        ),
        (
            "handler_without_adapter",
            (Route(name="h", group="read", handler="mod:attr", adapter=""),),
        ),
        (
            "prefix_without_argv",
            (Route(name="p", group="read", adapter="dispatcher", argv_prefix=("x",)),),
        ),
    ],
)
def test_each_extracted_check_still_fires(code: str, routes: tuple[Route, ...]) -> None:
    """Every rule that moved still detects its own violation through the public entry point.

    This is what makes the split checkable rather than merely importable: if a check were
    dropped from ``_CHECKS``, or its module-level vocabulary failed to move with it, the
    corresponding case here goes green-to-red.
    """
    assert code in _codes(routes)


def test_a_clean_table_produces_no_findings() -> None:
    """The negative control: the crafted-route harness is not reporting findings for free."""
    assert validate((Route(name="ok", group="read"),)) == ()
