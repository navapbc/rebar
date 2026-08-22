"""Behavioural contract of the shared nested-pytest launcher."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from _nested_pytest import BASETEMP_FLAG, nested_basetemp, run_nested_pytest
from _subprocess_env import subprocess_env

_PARENT_ONLY = "REBAR_NESTED_PYTEST_PARENT_ONLY"


def _probe(tmp_path: Path, body: str) -> Path:
    probe_dir = tmp_path / "probe"
    probe_dir.mkdir()
    probe = probe_dir / "test_probe.py"
    probe.write_text(body)
    return probe


def test_the_child_basetemp_is_owned_by_the_caller(tmp_path: Path) -> None:
    probe = _probe(tmp_path, "def test_uses_tmp(tmp_path):\n    (tmp_path / 'x').write_text('x')\n")

    result = run_nested_pytest(tmp_path, "-q", str(probe), env=subprocess_env(), timeout=300)

    assert result.returncode == 0, result.stdout + result.stderr
    basetemp = nested_basetemp(tmp_path)
    assert basetemp.is_dir()
    assert list(basetemp.rglob("x")), "the child did not allocate temp files under the basetemp"


def test_the_caller_environment_is_forwarded_verbatim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_PARENT_ONLY, "1")
    probe = _probe(
        tmp_path,
        "import os\n"
        "def test_env():\n"
        "    assert os.environ['REBAR_NESTED_SENTINEL'] == 'kept'\n"
        f"    assert {_PARENT_ONLY!r} not in os.environ\n",
    )
    child_env = {
        "REBAR_NESTED_SENTINEL": "kept",
        "PATH": str(Path(sys.executable).parent),
    }

    result = run_nested_pytest(tmp_path, "-q", str(probe), env=child_env, timeout=300)

    assert result.returncode == 0, result.stdout + result.stderr
    assert child_env == {"REBAR_NESTED_SENTINEL": "kept", "PATH": str(Path(sys.executable).parent)}


def test_a_tmp_path_that_does_not_exist_yet_still_gets_a_usable_basetemp(tmp_path: Path) -> None:
    """pytest's --basetemp does not create missing parents; the helper must."""
    probe = _probe(tmp_path, "def test_uses_tmp(tmp_path):\n    (tmp_path / 'x').write_text('x')\n")
    unborn = tmp_path / "not" / "created" / "yet"
    assert not unborn.exists()

    result = run_nested_pytest(unborn, "-q", str(probe), env=subprocess_env(), timeout=300)

    assert result.returncode == 0, result.stdout + result.stderr
    assert nested_basetemp(unborn).is_dir()


def test_a_timeout_propagates_to_the_caller(tmp_path: Path) -> None:
    # timing: hang-guard — asserts the raise only, never a wall-clock duration.
    probe = _probe(tmp_path, "import time\ndef test_hangs():\n    time.sleep(120)\n")

    with pytest.raises(subprocess.TimeoutExpired):
        run_nested_pytest(tmp_path, "-q", str(probe), env=subprocess_env(), timeout=5)


def test_the_call_contract_reaches_subprocess_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """env is passed through untouched, timeout is a keyword, the result is not re-wrapped."""
    captured: dict[str, object] = {}
    completed = subprocess.CompletedProcess(args=["pytest"], returncode=0, stdout="", stderr="")
    caller_env = {"PATH": "test-path"}

    def record(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return completed

    monkeypatch.setattr(subprocess, "run", record)
    result = run_nested_pytest(tmp_path, "-q", env=caller_env, timeout=30, cwd=None)

    assert result is completed
    assert captured["env"] is caller_env
    assert captured["timeout"] == 30
    assert captured["cwd"] is None
    launch = [sys.executable, "-m", "pytest"]  # nested-pytest-ok: an expectation, not a launch
    assert captured["command"][:3] == launch
    assert captured["command"][-2:] == [BASETEMP_FLAG, str(nested_basetemp(tmp_path))]


def test_the_cache_provider_suppression_is_optional(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[list[str]] = []
    completed = subprocess.CompletedProcess(args=["pytest"], returncode=0, stdout="", stderr="")
    monkeypatch.setattr(
        subprocess, "run", lambda command, **_: (seen.append(command), completed)[1]
    )

    run_nested_pytest(tmp_path, "-q", env={}, no_cacheprovider=True)
    run_nested_pytest(tmp_path, "-q", env={}, no_cacheprovider=False)

    assert seen[0][3:5] == ["-p", "no:cacheprovider"]
    assert "no:cacheprovider" not in seen[1]
