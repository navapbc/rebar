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
import subprocess
import sys

import pytest

from rebar import _deprecations as dep
from rebar._config_schema import _SECTIONS
from rebar.config import _LEGACY_ENV_ALIASES, _canonical_env_name

pytestmark = pytest.mark.unit

_GOLDEN = pathlib.Path(__file__).resolve().parents[1] / "golden" / "config_surface.json"
_SCRIPT = (
    pathlib.Path(__file__).resolve().parents[2] / "scripts" / "generate_config_surface_golden.py"
)


def _live_config_keys() -> set[str]:
    return {f"{s}.{k}" for s in _SECTIONS for k in _SECTIONS[s]}


def _live_env_vars() -> set[str]:
    return {_canonical_env_name(s, k) for s in _SECTIONS for k in _SECTIONS[s]}


def _tombstoned(kind: str) -> set[str]:
    return {ri.name for ri in dep.tombstones() if ri.kind == kind}


def _golden_data(path: pathlib.Path = _GOLDEN) -> dict[str, list[str]]:
    return json.loads(path.read_text(encoding="utf-8"))


def _orphaned_config_keys(data: dict[str, list[str]]) -> list[str]:
    # A retired config key is accounted for by a cfg tombstone OR an honored alias.
    from rebar._config_schema import _ALIASES

    aliases = {f"{sect}.{old}" for sect, m in _ALIASES.items() for old in m}
    accounted = _live_config_keys() | _tombstoned("cfg") | aliases
    return [k for k in data["config_keys"] if k not in accounted]


def _orphaned_env_vars(data: dict[str, list[str]]) -> list[str]:
    accounted = _live_env_vars() | _tombstoned("env") | set(_LEGACY_ENV_ALIASES)
    return [v for v in data["canonical_env_vars"] if v not in accounted]


def _missing_live_config_keys(data: dict[str, list[str]]) -> list[str]:
    enrolled = set(data["config_keys"])
    return sorted(key for key in _live_config_keys() if key not in enrolled)


def _missing_live_env_vars(data: dict[str, list[str]]) -> list[str]:
    enrolled = set(data["canonical_env_vars"])
    return sorted(name for name in _live_env_vars() if name not in enrolled)


def _run_generator(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *args],
        check=False,
        capture_output=True,
        text=True,
    )


def test_golden_file_present_and_shaped() -> None:
    data = _golden_data()
    assert isinstance(data.get("config_keys"), list)
    assert isinstance(data.get("canonical_env_vars"), list)


def test_no_config_key_removed_without_a_tombstone() -> None:
    orphaned = _orphaned_config_keys(_golden_data())
    assert not orphaned, (
        "config key(s) removed from the schema without a tombstone/alias entry "
        f"(add one in rebar._deprecations._TOMBSTONE_REGISTRY): {orphaned}"
    )


def test_no_env_var_removed_without_a_tombstone() -> None:
    orphaned = _orphaned_env_vars(_golden_data())
    assert not orphaned, (
        "canonical env var(s) removed without a tombstone/alias entry "
        f"(add one in rebar._deprecations._TOMBSTONE_REGISTRY): {orphaned}"
    )


def test_no_live_config_key_missing_from_golden() -> None:
    missing = _missing_live_config_keys(_golden_data())
    assert not missing, (
        f"live config key(s) must be enrolled in tests/golden/config_surface.json: {missing}"
    )


def test_no_live_env_var_missing_from_golden() -> None:
    missing = _missing_live_env_vars(_golden_data())
    assert not missing, (
        f"live canonical env var(s) must be enrolled in tests/golden/config_surface.json: {missing}"
    )


def test_generator_is_deterministic_and_check_clean(tmp_path: pathlib.Path) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    assert _run_generator("--output", str(first)).returncode == 0
    assert _run_generator("--output", str(second)).returncode == 0
    assert first.read_bytes() == second.read_bytes()
    clean = _run_generator("--check", "--output", str(first))
    assert clean.returncode == 0, clean.stderr
    assert clean.stdout == ""


@pytest.mark.parametrize(
    ("field", "missing_name"),
    [
        ("config_keys", "ticket.display_mode"),
        ("canonical_env_vars", "REBAR_TICKET_DISPLAY_MODE"),
    ],
)
def test_check_mode_fails_without_rewriting_when_live_name_missing(
    tmp_path: pathlib.Path,
    field: str,
    missing_name: str,
) -> None:
    path = tmp_path / "config_surface.json"
    data = _golden_data()
    data[field] = [name for name in data[field] if name != missing_name]
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    before = path.read_text(encoding="utf-8")
    result = _run_generator("--check", "--output", str(path))

    assert result.returncode == 1
    assert "is stale" in result.stderr
    assert path.read_text(encoding="utf-8") == before


def test_live_config_key_gap_is_detected_even_when_removals_are_accounted_for() -> None:
    data = _golden_data()
    data["config_keys"] = [name for name in data["config_keys"] if name != "ticket.display_mode"]
    assert _orphaned_config_keys(data) == []
    assert _missing_live_config_keys(data) == ["ticket.display_mode"]


def test_live_env_var_gap_is_detected_even_when_removals_are_accounted_for() -> None:
    data = _golden_data()
    data["canonical_env_vars"] = [
        name for name in data["canonical_env_vars"] if name != "REBAR_TICKET_DISPLAY_MODE"
    ]
    assert _orphaned_env_vars(data) == []
    assert _missing_live_env_vars(data) == ["REBAR_TICKET_DISPLAY_MODE"]
