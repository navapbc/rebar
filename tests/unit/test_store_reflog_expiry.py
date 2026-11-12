"""The write-locked maintenance step expires reflog entries past git's defaults.

Bug successive-deceptive-xiphias. Bounded git children run with gc off, and the explicit step
ran only commit-graph, loose-objects and incremental-repack, so nothing ever expired a reflog;
the production HEAD reflog had reached 4.69 MB / 25,933 entries. The step now runs
``git reflog expire --all`` and passes no expiry value of its own, so git's configured policy
(``gc.reflogExpire``/``gc.reflogExpireUnreachable``, defaults 90 and 30 days) decides.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path

import pytest
from _subprocess_env import SubprocessEnv, subprocess_env

from rebar._store import event_commit_git

pytestmark = pytest.mark.unit

_DAY = 86_400


@pytest.fixture
def git_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SubprocessEnv:
    """Isolate git from the host's config and give it a fixed identity."""
    empty = tmp_path / "empty-gitconfig"
    empty.write_text("")
    for name in [k for k in os.environ if k.startswith("GIT_")]:
        monkeypatch.delenv(name)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    return subprocess_env(
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@example.com",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@example.com",
    )


def _git(repo: Path, env: SubprocessEnv, *args: str, days_ago: float = 0) -> str:
    when = f"@{int(time.time() - days_ago * _DAY)} +0000"
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        env=env.with_overrides(GIT_COMMITTER_DATE=when, GIT_AUTHOR_DATE=when),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit(repo: Path, env: SubprocessEnv, msg: str, days_ago: float) -> str:
    _git(repo, env, "commit", "-q", "--allow-empty", "-m", msg, days_ago=days_ago)
    return _git(repo, env, "rev-parse", "HEAD")


def _reflog(repo: Path, env: SubprocessEnv) -> list[str]:
    return _git(repo, env, "log", "-g", "--format=%H", "HEAD").splitlines()


def _history(tmp_path: Path, env: SubprocessEnv, *config: str) -> tuple[Path, dict[str, str]]:
    """A repo whose HEAD reflog holds entries 120, 60, 45 and 1 day old, one unreachable."""
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], env=env, check=True)
    for item in config:
        key, value = item.split("=", 1)
        _git(repo, env, "config", key, value)
    commits = {
        "old": _commit(repo, env, "old", days_ago=120),
        "mid": _commit(repo, env, "mid", days_ago=60),
        "dropped": _commit(repo, env, "dropped", days_ago=45),
    }
    _git(repo, env, "reset", "-q", "--hard", commits["mid"], days_ago=45)
    commits["new"] = _commit(repo, env, "new", days_ago=1)
    assert set(commits.values()) <= set(_reflog(repo, env))
    return repo, commits


def _age_stamp(common: Path, *, git_dir: bool = True) -> None:
    """Record the last expiry pass as more than a day old, so the next write expires."""
    stamp = (common / ".git" if git_dir else common) / event_commit_git._REFLOG_EXPIRE_STAMP
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text("")
    then = time.time() - event_commit_git._REFLOG_EXPIRE_INTERVAL_S - 1
    os.utime(stamp, (then, then))


def test_stale_reflog_entries_are_expired_and_recent_ones_kept(tmp_path: Path, git_env) -> None:
    repo, commits = _history(tmp_path, git_env)
    _age_stamp(repo)

    event_commit_git.run_auto_maintenance(repo)

    after = _reflog(repo, git_env)
    assert commits["new"] in after, "a 1-day-old entry was expired"
    for name in ("old", "dropped"):
        assert commits[name] not in after, f"the {name} entry survived git's default expiry"


def test_expiry_honours_the_repositorys_own_reflog_settings(tmp_path: Path, git_env) -> None:
    # Proves the step passes no expiry value of its own: git's configured (or default) policy
    # decides, so an operator's gc.reflogExpire* still governs this store as it governs gc.
    repo, commits = _history(
        tmp_path, git_env, "gc.reflogExpire=never", "gc.reflogExpireUnreachable=never"
    )
    _age_stamp(repo)

    event_commit_git.run_auto_maintenance(repo)

    assert set(commits.values()) <= set(_reflog(repo, git_env))


@pytest.fixture
def expiry_due(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the step at a scratch common dir whose last expiry pass is over a day old."""
    common = tmp_path / "common-due"
    _age_stamp(common, git_dir=False)
    monkeypatch.setattr(event_commit_git, "_resolve_common_git_dir", lambda _t: str(common))
    return common


def _fake(results: dict[str, object], seen: list[tuple[str, ...]]):
    def fake_reaped(cwd, *args, timeout):
        seen.append(args)
        outcome = results[args[0]]
        if isinstance(outcome, BaseException):
            raise outcome
        return subprocess.CompletedProcess(["git", *args], outcome, "", "boom")

    return fake_reaped


@pytest.mark.parametrize(
    "reflog_outcome", [OSError("no git"), 124, 128], ids=["oserror", "timeout", "error"]
)
def test_a_failing_reflog_expiry_is_logged_never_raised(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, reflog_outcome, expiry_due
) -> None:
    seen: list[tuple[str, ...]] = []
    fake = _fake({"maintenance": 0, "reflog": reflog_outcome}, seen)
    monkeypatch.setattr(event_commit_git, "run_git_reaped", fake)

    with caplog.at_level(logging.WARNING, logger=event_commit_git.__name__):
        result = event_commit_git.run_auto_maintenance("/x")

    assert result is not None and result.returncode == 0
    assert [a[0] for a in seen] == ["maintenance", "reflog"]
    assert any("reflog expire" in r.getMessage() for r in caplog.records)


def test_reflog_expiry_is_skipped_after_a_timed_out_maintenance_run(
    monkeypatch: pytest.MonkeyPatch, expiry_due
) -> None:
    seen: list[tuple[str, ...]] = []
    monkeypatch.setattr(event_commit_git, "run_git_reaped", _fake({"maintenance": 124}, seen))

    result = event_commit_git.run_auto_maintenance("/x")

    assert result is not None and result.returncode == 124
    assert [a[0] for a in seen] == ["maintenance"]


def test_reflog_expiry_is_skipped_when_maintenance_cannot_launch(
    monkeypatch: pytest.MonkeyPatch, expiry_due
) -> None:
    seen: list[tuple[str, ...]] = []
    fake = _fake({"maintenance": OSError("no git")}, seen)
    monkeypatch.setattr(event_commit_git, "run_git_reaped", fake)

    assert event_commit_git.run_auto_maintenance("/x") is None
    assert [a[0] for a in seen] == ["maintenance"]


def test_reflog_expiry_runs_after_a_failed_but_finished_maintenance_run(
    monkeypatch: pytest.MonkeyPatch, expiry_due
) -> None:
    seen: list[tuple[str, ...]] = []
    fake = _fake({"maintenance": 128, "reflog": 0}, seen)
    monkeypatch.setattr(event_commit_git, "run_git_reaped", fake)

    result = event_commit_git.run_auto_maintenance("/x")

    assert result is not None and result.returncode == 128
    assert [a[0] for a in seen] == ["maintenance", "reflog"]


def test_a_timed_out_expiry_names_the_ref_locks_it_may_have_stranded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, git_env
) -> None:
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], env=git_env, check=True)
    stranded = repo / ".git" / "HEAD.lock"
    stranded.write_text("")
    _age_stamp(repo)
    seen: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        event_commit_git, "run_git_reaped", _fake({"maintenance": 0, "reflog": 124}, seen)
    )

    with caplog.at_level(logging.ERROR, logger=event_commit_git.__name__):
        event_commit_git.run_auto_maintenance(repo)

    assert any(str(stranded) in r.getMessage() for r in caplog.records)


def test_expiry_runs_at_most_once_a_day(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Each git start costs 0.1-1.3 s on a loaded host, and the opcert job's spawn ceiling
    # (test_opcert_service) caught a per-write expiry; git's shortest default expiry is 30 days.
    common = tmp_path / "common"
    _age_stamp(common, git_dir=False)
    monkeypatch.setattr(event_commit_git, "_resolve_common_git_dir", lambda _t: str(common))
    seen: list[tuple[str, ...]] = []
    fake = _fake({"maintenance": 0, "reflog": 0}, seen)
    monkeypatch.setattr(event_commit_git, "run_git_reaped", fake)

    event_commit_git.run_auto_maintenance("/x")
    event_commit_git.run_auto_maintenance("/x")
    assert [a[0] for a in seen].count("reflog") == 1, "a second write the same day re-expired"

    stamp = common / event_commit_git._REFLOG_EXPIRE_STAMP
    day_ago = time.time() - event_commit_git._REFLOG_EXPIRE_INTERVAL_S - 1
    os.utime(stamp, (day_ago, day_ago))
    event_commit_git.run_auto_maintenance("/x")
    assert [a[0] for a in seen].count("reflog") == 2, "a day-old pass did not re-expire"


def test_a_failed_expiry_is_retried_on_the_next_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    common = tmp_path / "common"
    _age_stamp(common, git_dir=False)
    stamp = common / event_commit_git._REFLOG_EXPIRE_STAMP
    aged = stamp.stat().st_mtime
    monkeypatch.setattr(event_commit_git, "_resolve_common_git_dir", lambda _t: str(common))
    seen: list[tuple[str, ...]] = []
    fake = _fake({"maintenance": 0, "reflog": 128}, seen)
    monkeypatch.setattr(event_commit_git, "run_git_reaped", fake)

    event_commit_git.run_auto_maintenance("/x")
    event_commit_git.run_auto_maintenance("/x")

    assert [a[0] for a in seen].count("reflog") == 2
    assert stamp.stat().st_mtime == aged, "a failed pass was recorded as done"


def test_a_store_with_no_record_starts_the_clock_instead_of_expiring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    common = tmp_path / "common"
    common.mkdir()
    monkeypatch.setattr(event_commit_git, "_resolve_common_git_dir", lambda _t: str(common))
    seen: list[tuple[str, ...]] = []
    monkeypatch.setattr(event_commit_git, "run_git_reaped", _fake({"maintenance": 0}, seen))

    event_commit_git.run_auto_maintenance("/x")

    assert [a[0] for a in seen] == ["maintenance"]
    assert (common / event_commit_git._REFLOG_EXPIRE_STAMP).exists()
