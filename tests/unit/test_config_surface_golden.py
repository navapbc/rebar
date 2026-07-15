"""CI anti-regression guard for the config surface (story 36c7).

The golden ``tests/golden/config_surface.json`` pins every config key + canonical env
var that rebar has exposed. This test loads it and fails if a pinned surface is in
NEITHER the live schema NOR the tombstone/alias registries — i.e. a key was removed
without leaving a fail-loud tombstone (or an honored alias). That is exactly the
silent-drop the tombstone registry exists to prevent, so it must break the build.

It intentionally does NOT fail when a NEW live key is absent from the golden (adding a
surface is not a regression); regenerate the golden deliberately when you add one.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from rebar import _deprecations as dep
from rebar._config_schema import _SECTIONS
from rebar.config import _LEGACY_ENV_ALIASES, _canonical_env_name

pytestmark = pytest.mark.unit

_GOLDEN = pathlib.Path(__file__).resolve().parents[1] / "golden" / "config_surface.json"


def _live_config_keys() -> set[str]:
    return {f"{s}.{k}" for s in _SECTIONS for k in _SECTIONS[s]}


def _live_env_vars() -> set[str]:
    return {_canonical_env_name(s, k) for s in _SECTIONS for k in _SECTIONS[s]}


def _tombstoned(kind: str) -> set[str]:
    return {ri.name for ri in dep.tombstones() if ri.kind == kind}


def test_golden_file_present_and_shaped() -> None:
    data = json.loads(_GOLDEN.read_text(encoding="utf-8"))
    assert isinstance(data.get("config_keys"), list)
    assert isinstance(data.get("canonical_env_vars"), list)


def test_no_config_key_removed_without_a_tombstone() -> None:
    data = json.loads(_GOLDEN.read_text(encoding="utf-8"))
    live = _live_config_keys()
    # A retired config key is accounted for by a cfg tombstone OR an honored alias.
    from rebar._config_schema import _ALIASES

    aliases = {f"{sect}.{old}" for sect, m in _ALIASES.items() for old in m}
    accounted = live | _tombstoned("cfg") | aliases
    orphaned = [k for k in data["config_keys"] if k not in accounted]
    assert not orphaned, (
        "config key(s) removed from the schema without a tombstone/alias entry "
        f"(add one in rebar._deprecations._TOMBSTONE_REGISTRY): {orphaned}"
    )


def test_no_env_var_removed_without_a_tombstone() -> None:
    data = json.loads(_GOLDEN.read_text(encoding="utf-8"))
    live = _live_env_vars()
    accounted = live | _tombstoned("env") | set(_LEGACY_ENV_ALIASES)
    orphaned = [v for v in data["canonical_env_vars"] if v not in accounted]
    assert not orphaned, (
        "canonical env var(s) removed without a tombstone/alias entry "
        f"(add one in rebar._deprecations._TOMBSTONE_REGISTRY): {orphaned}"
    )
