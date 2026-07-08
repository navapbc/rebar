"""Completeness + well-formedness tests for the central deprecation registry.

These guard the invariant introduced by ticket 5274 (ordinary-cloth-chipmunk):
``rebar._deprecations`` is the SINGLE source of truth for every deprecated
user-facing surface, and every runtime deprecation signal routes through
``warn_deprecated``. The source-scan test (``test_no_raw_deprecation_emissions``)
is the real, non-hollow check: it walks the shipped source and fails if any
``logger.warning(... deprecated ...)`` / ``warnings.warn(..., DeprecationWarning)``
emission bypasses the registry. A new deprecated surface added later that skips
the registry therefore breaks the build here.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

import rebar._deprecations as dep
from rebar._deprecations import REGISTRY, Deprecation, warn_deprecated

_SRC_ROOT = pathlib.Path(dep.__file__).resolve().parent

# Files allowed to contain raw deprecation-warning emissions: only the central
# helper module itself (which *is* the one place the message is built + emitted).
_EMISSION_ALLOWLIST = {"_deprecations.py"}


# ── (a) registry well-formedness ──────────────────────────────────────────────
def test_registry_entries_well_formed() -> None:
    assert REGISTRY, "the deprecation registry must not be empty"
    for key, entry in REGISTRY.items():
        assert isinstance(entry, Deprecation)
        # keyed by its own key (map integrity)
        assert entry.key == key
        # every entry names a surface + a replacement
        assert entry.name and entry.name.strip(), f"{key}: empty name"
        assert entry.replacement and entry.replacement.strip(), f"{key}: empty replacement"
        assert entry.kind in dep._KINDS, f"{key}: bad kind {entry.kind!r}"


def test_scheduled_have_horizon_permanent_have_none() -> None:
    for key, entry in REGISTRY.items():
        if entry.permanent:
            # a permanent alias is never scheduled for removal
            assert entry.remove_in is None, f"{key}: permanent entry must have remove_in=None"
        else:
            # a scheduled removal must record WHEN
            assert entry.remove_in, f"{key}: scheduled entry needs a non-empty remove_in"


def test_keys_are_unique() -> None:
    # REGISTRY is a dict keyed by .key, so re-derive from the source tuple to prove
    # no two entries collided (a dict would silently drop a duplicate).
    keys = [d.key for d in dep._ENTRIES]
    assert len(keys) == len(set(keys)), "duplicate deprecation keys"


def test_message_wording_depends_on_permanent() -> None:
    """A scheduled entry says 'deprecated'; a permanent one must NOT (AC4)."""
    for entry in REGISTRY.values():
        msg = dep._message(entry)
        assert entry.name in msg
        if entry.permanent:
            assert "deprecated" not in msg.lower(), f"{entry.key}: permanent must not warn"
            assert "permanent alias" in msg
        else:
            assert "deprecated" in msg.lower()
            assert entry.remove_in in msg


# ── (b) warn_deprecated raises on an unregistered key ─────────────────────────
def test_warn_deprecated_raises_on_unknown_key() -> None:
    assert "env:TOTALLY_MADE_UP_ALIAS" not in REGISTRY
    with pytest.raises(KeyError):
        warn_deprecated("env:TOTALLY_MADE_UP_ALIAS")


def test_warn_deprecated_returns_message_for_known_key() -> None:
    # sanity: a known key emits (does not raise) and returns the built message.
    key = next(iter(REGISTRY))
    msg = warn_deprecated(key)
    assert REGISTRY[key].name in msg


# ── (c) source-scan completeness: no raw deprecation emissions bypass the helper ─
def _is_deprecation_emission(node: ast.AST) -> bool:
    """True if ``node`` is a ``logger.warning(...)`` / ``warnings.warn(...)`` call that
    signals a deprecation — i.e. it passes ``DeprecationWarning`` or a string literal
    containing 'deprecat'. Docstrings/comments are ignored (only Call args count)."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    # only method-style emission calls: <x>.warn(...) / <x>.warning(...)
    if not (isinstance(func, ast.Attribute) and func.attr in {"warn", "warning"}):
        return False
    args = list(node.args) + [kw.value for kw in node.keywords]
    for a in args:
        # references DeprecationWarning (e.g. warnings.warn(msg, DeprecationWarning))
        if isinstance(a, ast.Name) and a.id == "DeprecationWarning":
            return True
        if isinstance(a, ast.Attribute) and a.attr == "DeprecationWarning":
            return True
        # a string-literal message that talks about deprecation
        if isinstance(a, ast.Constant) and isinstance(a.value, str):
            if "deprecat" in a.value.lower():
                return True
    return False


def _scan_emission_sites() -> list[str]:
    sites: list[str] = []
    for py in _SRC_ROOT.rglob("*.py"):
        if py.name in _EMISSION_ALLOWLIST:
            continue
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        for node in ast.walk(tree):
            if _is_deprecation_emission(node):
                rel = py.relative_to(_SRC_ROOT)
                sites.append(f"{rel}:{node.lineno}")
    return sites


def test_no_raw_deprecation_emissions() -> None:
    """Every runtime deprecation warning must go through ``warn_deprecated``; there
    must be NO raw ``.warning(...deprecated...)`` / ``warnings.warn(..., DeprecationWarning)``
    emission anywhere in the package except the central helper module."""
    sites = _scan_emission_sites()
    assert sites == [], (
        "raw deprecation-warning emission(s) bypass the central registry "
        "(route them through rebar._deprecations.warn_deprecated):\n  " + "\n  ".join(sites)
    )


def test_scanner_detects_a_planted_raw_emission(tmp_path: pathlib.Path) -> None:
    """The scanner is not hollow: it flags a raw emission it is shown."""
    planted = tmp_path / "planted.py"
    planted.write_text(
        "import logging, warnings\n"
        "logging.getLogger('x').warning('foo is deprecated; use bar')\n"
        "warnings.warn('baz', DeprecationWarning)\n"
        "# a comment mentioning deprecated must NOT count\n"
        "'''a docstring mentioning deprecated must NOT count'''\n",
        encoding="utf-8",
    )
    tree = ast.parse(planted.read_text(encoding="utf-8"))
    hits = [n for n in ast.walk(tree) if _is_deprecation_emission(n)]
    assert len(hits) == 2  # the two emission calls; not the comment or docstring
