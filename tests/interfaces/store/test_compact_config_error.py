"""`rebar compact` reports a malformed config as a clean error (exit 1), not an
uncaught traceback (session-review remediation: compact resolves its default
threshold via load_config, which can raise ConfigError on a broken config file).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
from rebar import config as cfg
from rebar._commands import compact

pytestmark = pytest.mark.unit


def test_compact_clean_error_on_malformed_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@e"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    monkeypatch.setenv("REBAR_SYNC_PULL", "off")
    monkeypatch.setenv("REBAR_SYNC_PUSH", "off")
    rebar.init_repo(repo_root=str(repo))
    tid = rebar.create_ticket("task", "Compactable", repo_root=str(repo))

    # Break the project config AFTER the ticket exists, then compact.
    (repo / "pyproject.toml").write_text("[tool.rebar] broken === [[\n", encoding="utf-8")
    cfg.reset_config_cache()
    capsys.readouterr()

    rc = compact.compact_cli([tid], repo_root=str(repo))
    err = capsys.readouterr().err
    assert rc == 1
    assert "Error:" in err  # clean message
    assert "Traceback" not in err  # NOT an uncaught traceback
