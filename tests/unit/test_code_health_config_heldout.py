"""Held-out CLI, validation, and golden-surface contracts for ticket e7a0."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from rebar import config


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / ".git").mkdir()
    return project


def test_cli_overrides_render_through_real_config_command(tmp_path: Path) -> None:
    project = _project(tmp_path)
    env = os.environ.copy()
    env.pop("REBAR_CONFIG", None)
    env["XDG_CONFIG_HOME"] = str(tmp_path / "xdg")
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "rebar",
            "-c",
            "code_health.enabled=true",
            "-c",
            "code_health.scan_roots=src,web",
            "-c",
            'code_health.analyzers={"python":"lizard","typescript":"jscpd"}',
            "-c",
            "code_health.size_cap=900",
            "-c",
            "code_health.size_near_fraction=0.2",
            "config",
            "--root",
            str(project),
            "--output",
            "json",
        ],
        cwd=project,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["config"]["code_health"] == {
        "enabled": True,
        "scan_roots": ["src", "web"],
        "analyzers": {"python": "lizard", "typescript": "jscpd"},
        "size_cap": 900,
        "size_near_fraction": 0.2,
    }
    assert payload["sources"]["code_health"] == {
        "enabled": "cli",
        "scan_roots": "cli",
        "analyzers": "cli",
        "size_cap": "cli",
        "size_near_fraction": "cli",
    }


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("size_near_fraction", 1.01, "must be <= 1.0"),
        ("analyzers", ["lizard"], "code_health.analyzers"),
    ],
)
def test_invalid_code_health_values_fail_at_load(
    key: str,
    value: object,
    message: str,
) -> None:
    with pytest.raises(config.ConfigError, match=message):
        config.Config.from_mapping({"code_health": {key: value}})


def test_config_surface_golden_includes_code_health_keys_and_env_names() -> None:
    golden = Path(__file__).parents[1] / "golden" / "config_surface.json"
    payload = json.loads(golden.read_text(encoding="utf-8"))

    assert {
        "code_health.enabled",
        "code_health.scan_roots",
        "code_health.analyzers",
        "code_health.size_cap",
        "code_health.size_near_fraction",
    } <= set(payload["config_keys"])
    assert {
        "REBAR_CODE_HEALTH_ENABLED",
        "REBAR_CODE_HEALTH_SCAN_ROOTS",
        "REBAR_CODE_HEALTH_ANALYZERS",
        "REBAR_CODE_HEALTH_SIZE_CAP",
        "REBAR_CODE_HEALTH_SIZE_NEAR_FRACTION",
    } <= set(payload["canonical_env_vars"])
