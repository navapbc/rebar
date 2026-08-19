from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from _subprocess_env import subprocess_env

pytestmark = pytest.mark.integration

_REPO_ROOT = Path(__file__).parents[2]
_FIXTURE_NODES = (
    "tests/integration/test_concurrency_regression.py::"
    "test_fixture_remote_runs_no_detached_upkeep_and_shares_no_objects",
    "tests/integration/test_prepare_reclaim_backup.py::"
    "test_prepares_manifest_bundle_and_exact_restore_without_remote_mutation",
    "tests/integration/test_reclaim_bridge_history.py::"
    "test_dry_run_preserves_graph_and_head_tree_without_publishing",
)


def _run_fixture_nodes(*, environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            *_FIXTURE_NODES,
        ],
        cwd=_REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def test_bare_repository_fixtures_are_portable_to_explicit_safety_policy(
    tmp_path: Path,
) -> None:
    strict_config = tmp_path / "strict-gitconfig"
    subprocess.run(
        [
            "git",
            "config",
            "--file",
            str(strict_config),
            "safe.bareRepository",
            "explicit",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    strict_environment = subprocess_env(
        GIT_CONFIG_GLOBAL=str(strict_config),
        GIT_CONFIG_NOSYSTEM="1",
    )

    strict = _run_fixture_nodes(environment=strict_environment)

    assert strict.returncode == 0, strict.stdout + strict.stderr
    assert "3 passed" in strict.stdout

    default_environment = subprocess_env(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
    )
    default = _run_fixture_nodes(environment=default_environment)
    assert default.returncode == 0, default.stdout + default.stderr
    assert "3 passed" in default.stdout

    missing = subprocess.run(
        ["git", "--git-dir", str(tmp_path / "missing.git"), "rev-parse", "HEAD"],
        env=strict_environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert missing.returncode == 128
    assert "not a git repository" in missing.stderr
