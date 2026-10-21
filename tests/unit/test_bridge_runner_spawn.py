"""The bridge runner spawns the engine directly and reaps its whole process group.

Story chippable-potty-dipper: ``rebar bridge run`` used to spawn ``rebar bridge
sync|preview``, which spawned the engine again with no timeout, so the runner's
3600 s timeout killed only the middle process and orphaned the engine (still holding
the pass lock). These tests pin the direct ``python -m rebar.reconciler`` argv, the
own-session spawn, the shared reaper on timeout, the absence of survivors after a real
timeout, and the runner's exit naming, which no longer re-canonicalises the child.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
import types
from pathlib import Path
from typing import ClassVar

import pytest

from rebar import _bridge_runner

pytestmark = pytest.mark.unit

POSIX_ONLY = pytest.mark.skipif(os.name != "posix", reason="process-group reaping is POSIX-only")

# Ceilings for the poll loops below: they make a broken spawn fail loudly instead of
# wedging the suite, and are never a pass condition.
_POLL_CEILING_SECONDS = 30.0
_POLL_INTERVAL_SECONDS = 0.01

# A child that starts a ``sleep 30`` grandchild (inheriting the stdout pipe), records
# both pids, signals readiness by creating its last argv element, then hangs itself.
_CHILD_WITH_GRANDCHILD = r"""
import os, subprocess, sys, time
grandchild = subprocess.Popen(["sleep", "30"])
with open(sys.argv[1], "w") as fh:
    fh.write(f"{os.getpid()} {grandchild.pid}")
open(sys.argv[2], "w").close()
time.sleep(30)
"""


class _FakePopen:
    """Records the spawn and plays back one ``communicate`` outcome."""

    calls: ClassVar[list[tuple[list[str], dict[str, object]]]] = []
    instances: ClassVar[list[_FakePopen]] = []
    outcome: object = ("", "")

    def __init__(self, argv, **kwargs):
        type(self).calls.append((list(argv), kwargs))
        type(self).instances.append(self)
        self.pid = 4242
        self.returncode = 0

    def communicate(self, timeout=None):
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def _patch_runner_popen(monkeypatch: pytest.MonkeyPatch, popen) -> None:
    """Swap ``Popen`` for the runner module only, never for the process-wide module."""
    runner_subprocess = types.SimpleNamespace(**vars(subprocess))
    runner_subprocess.Popen = popen
    monkeypatch.setattr(_bridge_runner, "subprocess", runner_subprocess)


@pytest.fixture
def fake_popen(monkeypatch: pytest.MonkeyPatch) -> type[_FakePopen]:
    fake = type("FakePopen", (_FakePopen,), {"calls": [], "instances": [], "outcome": ("", "")})
    _patch_runner_popen(monkeypatch, fake)
    return fake


@pytest.mark.parametrize(
    ("profile", "route_args"),
    [
        ("dry-run", ["preview"]),
        ("bootstrap-strict", ["sync", "--max-changes", "10"]),
        ("bootstrap-throttle", ["sync", "--max-changes", "100"]),
        ("live", ["sync"]),
    ],
)
def test_each_profile_spawns_the_engine_route_in_its_own_session(
    tmp_path: Path, fake_popen: type[_FakePopen], profile: str, route_args: list[str]
) -> None:
    argv = _bridge_runner._engine_argv(profile, tmp_path)

    _bridge_runner._spawn_bounded(argv, root=tmp_path, environ={"REBAR_ROOT": str(tmp_path)})

    [(spawned, kwargs)] = fake_popen.calls
    assert spawned == [
        sys.executable,
        "-m",
        "rebar.reconciler",
        *route_args,
        "--repo-root",
        str(tmp_path),
    ]
    assert kwargs["start_new_session"] is (os.name == "posix")
    assert kwargs["cwd"] == tmp_path
    assert kwargs["stdin"] is subprocess.DEVNULL


def test_a_timeout_reaps_the_child_through_the_shared_reaper(
    tmp_path: Path, fake_popen: type[_FakePopen], monkeypatch: pytest.MonkeyPatch
) -> None:
    reaped: list[tuple[object, dict[str, object]]] = []
    monkeypatch.setattr(
        _bridge_runner, "reap_process_group", lambda proc, **kw: reaped.append((proc, kw))
    )
    monkeypatch.setattr(_bridge_runner, "TIMEOUT_SECONDS", 1)
    fake_popen.outcome = subprocess.TimeoutExpired(["engine"], 1)

    with pytest.raises(subprocess.TimeoutExpired):
        _bridge_runner._spawn_bounded(
            _bridge_runner._engine_argv("live", tmp_path), root=tmp_path, environ={}
        )

    assert len(reaped) == 1
    proc, kwargs = reaped[0]
    assert proc is fake_popen.instances[0]
    assert kwargs["label"] == "bridge"
    assert kwargs["logger"] is _bridge_runner.logger


def _wait_until_gone(pids: list[int]) -> set[int]:
    """Poll ``kill(pid, 0)`` until every pid is gone; return the survivors."""
    remaining = set(pids)
    deadline = time.monotonic() + _POLL_CEILING_SECONDS
    while remaining and time.monotonic() < deadline:
        for pid in tuple(remaining):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                remaining.discard(pid)
        if remaining:
            time.sleep(_POLL_INTERVAL_SECONDS)
    return remaining


@POSIX_ONLY
def test_a_real_timeout_leaves_neither_the_child_nor_its_grandchild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pidfile = tmp_path / "pids"
    ready = tmp_path / "ready"
    real_popen = subprocess.Popen

    def ready_gated_popen(*args, **kwargs):
        # Block until the child has started its grandchild, so the 1 s window is spent
        # on the hang alone and never races interpreter start-up.
        process = real_popen(*args, **kwargs)
        deadline = time.monotonic() + _POLL_CEILING_SECONDS
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(_POLL_INTERVAL_SECONDS)
        return process

    _patch_runner_popen(monkeypatch, ready_gated_popen)
    monkeypatch.setattr(_bridge_runner, "TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(_bridge_runner, "_REAP_GRACE_S", 2.0)
    monkeypatch.setattr(_bridge_runner, "_REAP_DRAIN_S", 2.0)

    with pytest.raises(subprocess.TimeoutExpired):
        _bridge_runner._spawn_bounded(
            [sys.executable, "-c", _CHILD_WITH_GRANDCHILD, str(pidfile), str(ready)],
            root=tmp_path,
            environ=os.environ,
        )

    assert ready.exists(), "the child never started its grandchild"
    pids = [int(pid) for pid in pidfile.read_text(encoding="utf-8").split()]
    survivors = _wait_until_gone(pids)
    for pid in survivors:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
    assert not survivors, f"pids {sorted(survivors)} survived the process-group reap"


@pytest.mark.parametrize(
    ("returncode", "stderr", "expected"),
    [
        (0, "", ("converged", 0)),
        (0, "BRIDGE_STATE: converged\n", ("converged", 0)),
        (0, "noise\nBRIDGE_STATE: in-flight\n", ("in-flight", 0)),
        (0, "BRIDGE_STATE: legacy-gated\n", ("legacy-gated", 0)),
        (0, "BRIDGE_STATE: reschedule\n", ("reschedule", 0)),
        (0, "BRIDGE_STATE: no-such-state\n", ("converged", 0)),
        (0, "BRIDGE_STATE: in-flight\nBRIDGE_STATE: reschedule\n", ("converged", 0)),
        (1, "", ("operational_failure", 1)),
        (2, "", ("invalid_invocation", 2)),
        (1, "BRIDGE_STATE: in-flight\n", ("operational_failure", 1)),
        (3, "", ("operational_failure", 1)),
        (75, "", ("operational_failure", 1)),
        (-signal.SIGKILL, "", ("operational_failure", 1)),
    ],
)
def test_the_runner_names_the_state_without_recanonicalising_the_exit(
    returncode: int, stderr: str, expected: tuple[str, int]
) -> None:
    assert _bridge_runner._classified_result(returncode, stderr) == expected
