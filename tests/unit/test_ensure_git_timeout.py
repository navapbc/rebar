"""A git watchdog timeout stops the ensure sweep instead of being read as an answer.

Bug ``7bef-6755-be56-4a43`` (hilarious-loony-crayfish). With every tracker ``git config``
stalled, the units read each timed-out call's empty output as "unset", issued the write
(which also stalled), and reported ``changed``: 7 sequential bounded calls in one sweep, so
a few minutes of host stall pushed ``rebar init`` past a 300 s test budget, and the
outcome claimed convergence for writes that never happened.

The git seam is stubbed to return the watchdog's own result (code 124), so no test sleeps.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

import rebar
from rebar._commands import _init_ensures, init
from rebar._store import ensures
from rebar._store.git_outcome import WATCHDOG_TIMEOUT_RC, GitTimedOut

pytestmark = pytest.mark.unit


def _timed_out(args: tuple[str, ...]) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        ["git", *args], WATCHDOG_TIMEOUT_RC, "", "git timed out after 120s"
    )


def _stall_config(real: Callable[..., subprocess.CompletedProcess], calls: list[tuple]):
    """A ``run_git_write`` stand-in whose ``config`` calls time out; others run for real."""

    def run(cwd, *args, **kwargs):
        calls.append(args)
        if args[:1] == ("config",):
            return _timed_out(args)
        return real(cwd, *args, **kwargs)

    return run


def _git_repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)
    return path


@pytest.fixture
def tracker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "t@e.com"),
        ("git", "config", "user.name", "t"),
        ("git", "commit", "-q", "--allow-empty", "-m", "i"),
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    return str(repo / ".tickets-tracker")


def test_gc_config_stops_at_its_first_timed_out_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _git_repo(tmp_path / "r")
    calls: list[tuple] = []
    monkeypatch.setattr(
        _init_ensures, "run_git_write", _stall_config(_init_ensures.run_git_write, calls)
    )
    with pytest.raises(GitTimedOut) as raised:
        _init_ensures._gc_config_unit(str(repo))
    assert len(calls) == 1, f"a timed-out read must not lead to more git calls: {calls}"
    assert raised.value.operation == "config --get"


def test_the_sweep_stops_at_a_timeout_and_claims_no_later_unit(
    tracker: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran_later: list[str] = []
    for uid in ensures.REGISTRY_IDS[2:]:
        name = {
            "merge-ours": "_merge_ours_unit",
            "gitattributes": "_gitattributes_unit",
            "gitignore": "_gitignore_unit",
            "store-compat": "_store_compat_unit",
            "untrack-runtime-markers": "_untrack_runtime_markers_unit",
        }.get(uid)
        if name:
            monkeypatch.setattr(init, name, lambda t, uid=uid: ran_later.append(uid))
    calls: list[tuple] = []
    monkeypatch.setattr(
        _init_ensures, "run_git_write", _stall_config(_init_ensures.run_git_write, calls)
    )

    outcomes = ensures.run_ensures(tracker)

    assert [o.id for o in outcomes] == ["env-id", "gc-config"]
    gc = outcomes[1]
    assert gc.status == "failed"
    assert "config --get" in gc.detail and "8 later unit(s) not run" in gc.detail
    assert ran_later == []
    assert ensures.applied_ids(tracker) == {"env-id"}


def test_the_next_sweep_converges_the_units_a_timeout_skipped(
    tracker: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple] = []
    with monkeypatch.context() as stalled:
        stalled.setattr(
            _init_ensures, "run_git_write", _stall_config(_init_ensures.run_git_write, calls)
        )
        ensures.run_ensures(tracker)
    assert ensures.applied_ids(tracker) == {"env-id"}

    outcomes = ensures.run_ensures(tracker)

    assert [o for o in outcomes if o.status == "failed"] == []
    assert ensures.applied_ids(tracker) == set(ensures.REGISTRY_IDS)


def test_untrack_runtime_markers_does_not_read_a_timed_out_ls_files_as_none(
    tracker: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = init._git

    def git(cwd, *args):
        return _timed_out(args) if args[:1] == ("ls-files",) else real(cwd, *args)

    monkeypatch.setattr(init, "_git", git)
    with pytest.raises(GitTimedOut):
        init._untrack_runtime_markers_unit(tracker)


@pytest.mark.parametrize(
    ("unit", "uid"),
    [(_init_ensures._gc_config_unit, "gc-config"), (_init_ensures._merge_ours_unit, "merge-ours")],
)
def test_a_failed_config_write_is_reported_failed_not_changed(
    tmp_path: Path, unit: Callable[[str], ensures.EnsureOutcome], uid: str
) -> None:
    repo = _git_repo(tmp_path / "r")
    Path(repo, ".git", "config.lock").write_text("", encoding="utf-8")  # a held config lock
    outcome = unit(str(repo))
    assert outcome.id == uid
    assert outcome.status == "failed"
    assert "could not lock config file" in outcome.detail


def test_the_bootstrap_pin_stops_after_one_timed_out_call_and_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The pin no longer swallows the timeout: every later bootstrap call would wait out the
    # same bound, so init_core stops there (bug corrupting-plaster-seagull).
    repo = _git_repo(tmp_path / "r")
    calls: list[tuple] = []
    monkeypatch.setattr(
        _init_ensures, "run_git_write", _stall_config(_init_ensures.run_git_write, calls)
    )
    with pytest.raises(GitTimedOut):
        init._pin_auto_maintenance(str(repo))
    assert len(calls) == 1


def test_any_other_unit_error_is_still_recorded_and_the_sweep_continues(
    tracker: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_tracker: str) -> ensures.EnsureOutcome:
        raise ValueError("boom")

    monkeypatch.setattr(init, "_gitattributes_unit", boom)
    outcomes = {o.id: o for o in ensures.run_ensures(tracker)}
    assert outcomes["gitattributes"].status == "failed"
    assert outcomes["gitattributes"].detail == "boom"
    assert set(outcomes) == set(ensures.REGISTRY_IDS)
    assert os.path.exists(os.path.join(tracker, ensures.APPLIED_MARKER))
