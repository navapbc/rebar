"""Happy-path contracts for the typed ``[code_health]`` configuration."""

from __future__ import annotations

from pathlib import Path

from rebar import config


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / ".git").mkdir()
    return project


def test_missing_section_uses_inert_code_health_defaults(tmp_path: Path) -> None:
    assert hasattr(config, "CodeHealthConfig"), "CodeHealthConfig is not publicly re-exported"

    value = config.load_config(root=_project(tmp_path)).code_health

    assert value.enabled is False
    assert value.scan_roots == []
    assert value.analyzers == {}
    assert value.size_cap is None
    assert value.size_near_fraction == 0.1


def test_code_health_toml_values_are_typed(tmp_path: Path) -> None:
    project = _project(tmp_path)
    (project / "rebar.toml").write_text(
        "[code_health]\n"
        "enabled = true\n"
        'scan_roots = ["src", "web"]\n'
        'analyzers = { python = "lizard", typescript = "jscpd" }\n'
        "size_cap = 800\n"
        "size_near_fraction = 0.15\n",
        encoding="utf-8",
    )

    value = config.load_config(root=project).code_health

    assert value.enabled is True
    assert value.scan_roots == ["src", "web"]
    assert value.analyzers == {"python": "lizard", "typescript": "jscpd"}
    assert value.size_cap == 800
    assert value.size_near_fraction == 0.15
