"""Behavioural contract of the shared nested-pytest launcher."""

from __future__ import annotations

import ntpath
import os
import subprocess
import sys
from pathlib import Path

import pytest
from _nested_pytest import (
    BASETEMP_FLAG,
    _child_env,
    _rootdir_pin,
    nested_basetemp,
    run_nested_pytest,
)
from _subprocess_env import SubprocessEnv, subprocess_env

_PARENT_ONLY = "REBAR_NESTED_PYTEST_PARENT_ONLY"


def _probe(tmp_path: Path, body: str) -> Path:
    probe_dir = tmp_path / "probe"
    probe_dir.mkdir()
    probe = probe_dir / "test_probe.py"
    probe.write_text(body, encoding="utf-8")
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
    """pytest does not create the basetemp option's missing parents; the helper must."""
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
    """env is passed through untouched (on Windows, plus the OS-required SYSTEMROOT), timeout
    is a keyword, and the result is not re-wrapped."""
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
    if os.name == "nt":
        assert captured["env"] == _child_env(caller_env)
    else:
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


_WINDOWS_AMBIENT = {"SYSTEMROOT": r"C:\Windows", "SECRET": "x"}


def test_a_windows_child_gains_only_systemroot() -> None:
    """Winsock (so asyncio, so anyio's pytest11 plugin) cannot start without SYSTEMROOT."""
    caller_env = {"PATH": "p"}

    child = _child_env(caller_env, os_name="nt", ambient=_WINDOWS_AMBIENT)

    assert child == {"PATH": "p", "SYSTEMROOT": r"C:\Windows"}
    assert caller_env == {"PATH": "p"}, "the caller's mapping must not be mutated"


def test_a_posix_child_gets_the_callers_object_unchanged() -> None:
    caller_env = {"PATH": "p"}

    assert _child_env(caller_env, os_name="posix", ambient=_WINDOWS_AMBIENT) is caller_env


@pytest.mark.parametrize("key", ["SYSTEMROOT", "SystemRoot"])
def test_a_caller_supplied_systemroot_is_kept_as_given(key: str) -> None:
    caller_env = {"PATH": "p", key: r"D:\Win"}

    assert _child_env(caller_env, os_name="nt", ambient=_WINDOWS_AMBIENT) is caller_env


def test_nothing_is_added_when_the_ambient_environment_has_no_systemroot() -> None:
    caller_env = {"PATH": "p"}

    assert _child_env(caller_env, os_name="nt", ambient={"SECRET": "x"}) is caller_env


def test_a_redacting_mapping_stays_redacting() -> None:
    caller_env = SubprocessEnv({"PATH": "p", "TOKEN": "s3cret"})

    child = _child_env(caller_env, os_name="nt", ambient=_WINDOWS_AMBIENT)

    assert isinstance(child, SubprocessEnv)
    assert "s3cret" not in repr(child)
    assert "SYSTEMROOT" not in caller_env


# ── rootdir pin: a child must not walk the shared temp dir (bug refractive-hasty-iguanodon) ──
_WIN_REPO = "D:\\a\\rebar\\rebar"
_WIN_DIR = (
    "C:\\Users\\runneradmin\\AppData\\Local\\Temp\\pytest-of-runneradmin\\pytest-0\\popen-gw2\\t0"
)
_WIN_TEST = f"{_WIN_DIR}\\test_render_fixture.py"
_WIN_EXISTING = {_WIN_DIR, _WIN_TEST, "E:\\other", "E:\\other\\test_y.py", "C:\\x"}


def _win_pin(args: list[str]) -> list[str]:
    """The pin as the Windows sweep's child computes it, evaluated on any host."""
    return _rootdir_pin(
        args,
        _WIN_REPO,
        pathmod=ntpath,
        # Windows paths are case-insensitive, so existence is too; otherwise a case-changed
        # argument would read as missing and never reach the under-cwd check.
        exists=lambda p: (
            ntpath.normcase(p) in {ntpath.normcase(e) for e in _WIN_EXISTING}
            or ntpath.normcase(p).startswith(ntpath.normcase(_WIN_REPO))
        ),
        isdir=lambda p: not p.lower().endswith(".py"),
    )


def test_a_windows_tmp_path_test_on_another_drive_pins_its_own_directory() -> None:
    """The Windows CI layout: cwd on ``D:``, test file under ``%TEMP%`` on ``C:``. Unpinned,
    pytest keeps ``D:`` as rootdir and collection lstats every entry of ``C:`` ancestors."""
    assert _win_pin(["--tb=short", "-q", _WIN_TEST]) == ["--rootdir", _WIN_DIR]


def test_a_node_id_pins_the_directory_of_its_file() -> None:
    assert _win_pin([f"{_WIN_TEST}::test_render_fixture"]) == ["--rootdir", _WIN_DIR]


def test_a_directory_argument_pins_itself() -> None:
    assert _win_pin(["-q", _WIN_DIR]) == ["--rootdir", _WIN_DIR]


def test_a_case_changed_argument_still_counts_as_existing() -> None:
    """Guards the fake above: the case-changed under-cwd argument must reach ``_is_within``."""
    upper = _WIN_TEST.upper()

    assert _win_pin(["-q", upper]) == ["--rootdir", ntpath.dirname(upper)]


def test_an_option_value_that_is_not_a_path_is_ignored() -> None:
    assert _win_pin(["-k", "test_x or test_y", "-p", "no:randomly", _WIN_TEST]) == [
        "--rootdir",
        _WIN_DIR,
    ]


@pytest.mark.parametrize(
    "args",
    [
        pytest.param(["-q", "tests/unit/test_x.py"], id="relative"),
        pytest.param(["-q", f"{_WIN_REPO}\\tests\\unit\\test_x.py"], id="under-cwd"),
        pytest.param(["-q", "d:\\A\\Rebar\\rebar\\tests\\unit\\test_x.py"], id="under-cwd-case"),
        pytest.param(["-q"], id="no-path"),
        pytest.param(["-q", "C:\\gone\\test_x.py"], id="missing-path"),
        pytest.param(["--rootdir", "C:\\x", _WIN_TEST], id="rootdir"),
        pytest.param(["--rootdir=C:\\x", _WIN_TEST], id="rootdir-joined"),
        pytest.param(["-c", "C:\\x\\pytest.ini", _WIN_TEST], id="c"),
        pytest.param(["-cC:\\x\\pytest.ini", _WIN_TEST], id="c-attached"),
        pytest.param(["--config-file=C:\\x\\pytest.ini", _WIN_TEST], id="config-file"),
        pytest.param(["--confcutdir", "C:\\x", _WIN_TEST], id="confcutdir"),
        pytest.param([_WIN_TEST, "E:\\other\\test_y.py"], id="two-drives"),
        pytest.param([_WIN_TEST, f"{_WIN_REPO}\\tests\\test_x.py"], id="mixed-in-and-out"),
    ],
)
def test_the_pin_is_left_out_when_pytest_already_roots_the_session_correctly(
    args: list[str],
) -> None:
    assert _win_pin(args) == []


def test_a_posix_test_file_outside_the_cwd_pins_its_own_directory(tmp_path: Path) -> None:
    probe = _probe(tmp_path, "def test_ok():\n    pass\n")

    assert _rootdir_pin(["-q", str(probe)], tmp_path / "cwd") == ["--rootdir", str(probe.parent)]


def test_the_child_does_not_collect_above_its_test_files_directory(tmp_path: Path) -> None:
    """End to end on any OS: the child's cwd and its test file share a non-root ancestor that
    holds a conftest.py raising at import. Unpinned, pytest roots the session at that shared
    ancestor, walks it, and imports the conftest; pinned, the walk starts at the test file's
    own directory, which is what keeps a Windows child out of ``%TEMP%``'s siblings."""
    shared = tmp_path.resolve()
    walked = "raise RuntimeError('collection walked above the test dir')\n"
    (shared / "conftest.py").write_text(walked, encoding="utf-8")
    cwd = shared / "cwd"
    cwd.mkdir()
    probe = _probe(shared, "def test_ok():\n    pass\n")

    result = run_nested_pytest(
        shared / "run", "-q", str(probe), env=subprocess_env(), timeout=300, cwd=cwd
    )

    assert result.returncode == 0, result.stdout + result.stderr
