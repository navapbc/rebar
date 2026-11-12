"""fsck surfaces PUSH_PENDING when the local tickets branch is ahead of origin.

In-process port of tests/scripts/test-ticket-fsck-push-pending.sh (the bash engine
is being deleted). Push is best-effort, so a local commit with no push silently
diverges from origin; fsck must surface that (it is informational — it does NOT
fail the fsck). Drives ``rebar._cli.main(["store", "fsck"])`` against a tracker ahead of a
real local bare origin.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from _subprocess_env import subprocess_env

import rebar
from rebar import _cli
from rebar._store import gitutil


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _git_q(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)


@pytest.fixture
def repo_with_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Path, Path, str]]:
    """Initialized rebar repo wired to a real local bare origin; origin/tickets
    seeded so divergence is observable. Yields (repo, tracker)."""
    origin = tmp_path / "origin.git"
    repo = tmp_path / "work"
    subprocess.run(
        ["git", "init", "-q", "--bare", str(origin)],
        check=True,
        capture_output=True,
        text=True,
    )
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    _git("config", "user.email", "t@t.co", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    _git("remote", "add", "origin", str(origin), cwd=repo)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))

    tracker = repo / ".tickets-tracker"
    # Seed origin/tickets (REBAR_SYNC_PUSH=always) so a later un-pushed commit diverges.
    monkeypatch.setenv("REBAR_SYNC_PUSH", "always")
    seed = rebar.create_ticket("task", "seed", repo_root=str(repo))
    _git_q("fetch", "origin", "tickets", cwd=tracker)
    return repo, tracker, seed


def _ahead(tracker: Path) -> int:
    cp = _git_q("rev-list", "origin/tickets..HEAD", "--count", cwd=tracker)
    return int((cp.stdout or "0").strip() or "0")


def test_fsck_reports_push_pending_and_stays_exit_0(
    repo_with_origin: tuple[Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, tracker, _seed = repo_with_origin
    # A local-only commit: push off so origin does not advance.
    monkeypatch.setenv("REBAR_SYNC_PUSH", "off")
    rebar.create_ticket("task", "unpushed local ticket", repo_root=str(repo))
    _git_q("fetch", "origin", "tickets", cwd=tracker)
    assert _ahead(tracker) >= 1, "fixture did not reach a local-ahead state"

    rc = _cli.main(["store", "fsck"])
    out = capsys.readouterr().out

    assert "PUSH_PENDING" in out, f"fsck did not surface PUSH_PENDING; output:\n{out}"
    # Informational only — must not turn a clean fsck into a failure.
    assert rc == 0, f"PUSH_PENDING should not be an integrity failure (exit {rc})"


def test_fsck_quiet_when_in_sync(
    repo_with_origin: tuple[Path, Path, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _repo, tracker, _seed = repo_with_origin
    # Push HEAD to origin so local and origin/tickets are level.
    _git_q("push", "origin", "HEAD:tickets", cwd=tracker)
    _git_q("fetch", "origin", "tickets", cwd=tracker)
    assert _ahead(tracker) == 0, "fixture unexpectedly ahead of origin"

    rc = _cli.main(["store", "fsck"])
    out = capsys.readouterr().out
    # The absence below also holds for an fsck that failed before checking anything.
    assert rc == 0, f"fsck failed on an in-sync store (exit {rc}):\n{out}"
    assert "fsck complete: no issues found" in out, f"fsck did not finish its checks:\n{out}"
    assert "PUSH_PENDING" not in out, f"fsck emitted PUSH_PENDING when in sync:\n{out}"


# ── DIVERGED: a store that cannot fast-forward onto origin/tickets (bug 01e8) ─────────
#
# When init could not see origin/tickets (a single-branch clone) it built a fresh orphan
# store; _store/sync.py refuses to absorb an orphan that carries events and LOGS a
# best-effort "DIVERGED" warning, but that warning is invisible to an operator who runs
# the dedicated health check. fsck must surface divergence as a REAL, counted issue —
# not mislabel it as the informational PUSH_PENDING "ahead by N" (which tells the
# operator to just retry a write, when in fact the store will never fast-forward).


def _assert_named_commands_exist(line: str, capsys: pytest.CaptureFixture[str]) -> None:
    """Every `rebar ...` command the advice names must parse as a real subcommand."""
    commands = re.findall(r"`rebar ([^`]+)`", line)
    assert commands, f"the advice names no rebar command: {line}"
    for command in commands:
        words = [w for w in command.split() if not w.startswith("-")]
        try:
            code = _cli.main([*words, "--help"])
        except SystemExit as exc:  # argparse exits 2 on an unknown subcommand
            code = exc.code
        capsys.readouterr()
        assert code == 0, f"`rebar {command}` is not a rebar command (exit {code})"


def test_fsck_reports_diverged_on_unrelated_history(
    repo_with_origin: tuple[Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _repo, tracker, _seed = repo_with_origin
    monkeypatch.setenv("REBAR_SYNC_PUSH", "off")
    # Rewrite the local tracker onto an orphan branch that shares NO history with
    # origin/tickets — exactly the divergent empty/independent store bug 01e8 produced.
    _git("checkout", "-q", "--orphan", "diverged-orphan", cwd=tracker)
    _git_q("rm", "-rfq", ".", cwd=tracker)
    (tracker / "unrelated.txt").write_text("independently built store\n", encoding="utf-8")
    _git("add", "-A", cwd=tracker)
    _git("commit", "-qm", "orphan store root (no common ancestor with origin)", cwd=tracker)
    _git("branch", "-qM", "tickets", cwd=tracker)
    assert _git_q("merge-base", "HEAD", "origin/tickets", cwd=tracker).returncode != 0, (
        "precondition: local and origin/tickets must share no common ancestor"
    )

    rc = _cli.main(["store", "fsck"])
    out = capsys.readouterr().out

    assert "DIVERGED" in out, f"fsck did not surface DIVERGED on an unrelated history:\n{out}"
    assert "PUSH_PENDING" not in out, f"fsck mislabeled a divergent store as push-pending:\n{out}"
    assert rc == 1, f"DIVERGED must be a counted integrity issue (non-zero exit); got {rc}"
    line = next(ln for ln in out.splitlines() if "DIVERGED" in ln)
    assert "shares no history" in line, line
    _assert_named_commands_exist(line, capsys)


def test_fsck_reports_diverged_on_non_fast_forward(
    repo_with_origin: tuple[Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from rebar._commands import fsck

    _repo, tracker, _seed = repo_with_origin
    monkeypatch.setenv("REBAR_SYNC_PUSH", "off")
    # Advance origin/tickets by one commit the local tracker never sees, then make a
    # divergent local commit atop the shared base: common ancestor exists, but neither
    # side is an ancestor of the other → a non-fast-forwardable divergence. Assert the
    # classifier directly (the full CLI would trigger a best-effort reconverge that
    # union-merges a common-ancestor divergence, which is correct auto-healing and would
    # mask the transient state this check reports when a fetch cannot heal it, e.g.
    # offline).
    base = _git_q("rev-parse", "HEAD", cwd=tracker).stdout.strip()
    _git("checkout", "-q", "-b", "remote-advance", cwd=tracker)
    (tracker / "remote-only.txt").write_text("origin-side commit\n", encoding="utf-8")
    _git("add", "-A", cwd=tracker)
    _git("commit", "-qm", "origin-side divergent commit", cwd=tracker)
    _git_q("push", "-f", "origin", "HEAD:tickets", cwd=tracker)
    _git("checkout", "-q", "tickets", cwd=tracker)
    _git_q("reset", "--hard", base, cwd=tracker)
    (tracker / "local-only.txt").write_text("local-side commit\n", encoding="utf-8")
    _git("add", "-A", cwd=tracker)
    _git("commit", "-qm", "local-side divergent commit", cwd=tracker)
    _git_q("fetch", "origin", "tickets", cwd=tracker)
    assert _git_q("merge-base", "HEAD", "origin/tickets", cwd=tracker).returncode == 0, (
        "precondition: a common ancestor must exist"
    )
    assert (
        _git_q("merge-base", "--is-ancestor", "origin/tickets", "HEAD", cwd=tracker).returncode != 0
    ), "precondition: origin/tickets must NOT be an ancestor of HEAD (a real divergence)"

    line, is_issue = fsck._tracker_sync_status(str(tracker))

    assert line is not None and "DIVERGED" in line, (
        f"classifier did not report a non-fast-forward divergence; got {line!r}"
    )
    assert is_issue is True, "a non-fast-forward divergence must be a counted issue"
    # A related divergence keeps its shared history: the advice must say so, count each side,
    # and steer away from re-cloning, which would discard the local-only commit (bug 4f16-c86f).
    assert "share history" in line and "no history" not in line, line
    assert "(1 local-only, 1 remote-only commit(s))" in line, line
    assert "Do not re-clone" in line, line
    _assert_named_commands_exist(line, capsys)


def test_first_write_refuses_unrelated_diverged_store(
    repo_with_origin: tuple[Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, tracker, seed = repo_with_origin
    monkeypatch.setenv("REBAR_SYNC_PUSH", "always")
    _git("checkout", "-q", "--orphan", "independent-ticket-store", cwd=tracker)
    _git("add", "-A", cwd=tracker)
    _git("commit", "-qm", "independently rebuilt ticket store", cwd=tracker)
    _git("branch", "-qM", "tickets", cwd=tracker)
    _git_q("fetch", "origin", "tickets", cwd=tracker)
    before = _git_q("rev-parse", "HEAD", cwd=tracker).stdout.strip()

    with pytest.raises(rebar.RebarError) as exc:
        rebar.comment(seed, "this write must not be stranded", repo_root=str(repo))

    after = _git_q("rev-parse", "HEAD", cwd=tracker).stdout.strip()
    assert after == before, "the refusal must happen before committing a stranded event"
    msg = str(exc.value)
    assert "ticket write refused" in msg
    assert "DIVERGED" in msg
    assert "fsck-recover" in msg


def _ambient_repo(path: Path, *, sync_remote: str) -> Path:
    """An unrelated checkout whose ``sync_remote`` points somewhere else entirely."""
    path.mkdir()
    env = subprocess_env()
    for args in (("init", "-q"), ("remote", "add", sync_remote, str(path / "elsewhere.git"))):
        subprocess.run(["git", *args], cwd=path, check=True, capture_output=True, env=env)
    if sync_remote != "origin":
        (path / "rebar.toml").write_text(f'[sync]\nremote = "{sync_remote}"\n')
    return path


@pytest.mark.parametrize("ambient_sync_remote", ["origin", "upstream"])
def test_split_store_write_is_refused_when_cwd_repo_is_unrelated(
    repo_with_origin: tuple[Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    ambient_sync_remote: str,
) -> None:
    """cliffy-nonliving-blacklab: the guard follows the write's ``repo_root``.

    With ``REBAR_ROOT`` at an unrelated repo, the guard used to read that repo's remote:
    a different URL (``origin``) or a remote the tracker lacks (``upstream``) made it
    allow the write, stranding the event on a history with no common ancestor.
    """
    repo, tracker, seed = repo_with_origin
    monkeypatch.setenv("REBAR_SYNC_PUSH", "always")
    _git("checkout", "-q", "--orphan", "independent-ticket-store", cwd=tracker)
    _git("add", "-A", cwd=tracker)
    _git("commit", "-qm", "independently rebuilt ticket store", cwd=tracker)
    _git("branch", "-qM", "tickets", cwd=tracker)
    _git_q("fetch", "origin", "tickets", cwd=tracker)
    assert _git_q("merge-base", "HEAD", "origin/tickets", cwd=tracker).returncode != 0
    ambient = _ambient_repo(tmp_path / "ambient", sync_remote=ambient_sync_remote)
    monkeypatch.setenv("REBAR_ROOT", str(ambient))
    before = _git_q("rev-parse", "HEAD", cwd=tracker).stdout.strip()

    with pytest.raises(rebar.RebarError) as exc:
        rebar.comment(seed, "this write must not be stranded", repo_root=str(repo))

    after = _git_q("rev-parse", "HEAD", cwd=tracker).stdout.strip()
    assert after == before, "the refusal must happen before committing a stranded event"
    assert "ticket write refused" in str(exc.value)
    assert "origin/tickets" in str(exc.value)


def test_push_pending_names_the_configured_sync_remote(
    repo_with_origin: tuple[Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The hint names the remote the store actually syncs to, not always ``origin``."""
    repo, tracker, _seed = repo_with_origin
    origin_url = _git_q("remote", "get-url", "origin", cwd=repo).stdout.strip()
    _git_q("remote", "add", "s3tickets", origin_url, cwd=repo)
    _git_q("fetch", "s3tickets", "tickets", cwd=tracker)
    monkeypatch.setenv("REBAR_SYNC_REMOTE", "s3tickets")
    monkeypatch.setenv("REBAR_SYNC_PUSH", "off")
    rebar.create_ticket("task", "unpushed local ticket", repo_root=str(repo))

    _cli.main(["store", "fsck"])
    out = capsys.readouterr().out

    assert "PUSH_PENDING" in out, out
    assert "check connectivity to s3tickets" in out, out


# ── a watchdog timeout is not a history verdict (bug unarmed-raw-siskin) ───────────────
# ``_tracker_sync_status`` reads git's return codes as ancestry answers. A ``merge-base``
# killed by fsck's watchdog must be reported as a timeout naming the operation and its
# bound — never as DIVERGED, whose fsck-recover / re-clone advice cannot fix a slow or
# wedged git call and, for re-clone, risks discarding local-only commits.


def _time_out_git(monkeypatch: pytest.MonkeyPatch, *, when) -> None:
    """Make every real ``git`` call whose argv satisfies *when* raise ``TimeoutExpired``,
    as the 120 s watchdog would; every other call runs for real."""
    real_run = subprocess.run

    def fake_run(argv, *args, **kwargs):
        if isinstance(argv, list) and argv[:1] == ["git"] and when(argv):
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout") or 0)
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(gitutil, "_run_in_group", fake_run)


def _assert_timeout_report(line: str | None, is_issue: bool) -> None:
    from rebar._commands import fsck, fsck_tracker_health

    assert line is not None, "a timed-out git call must be reported, not silently dropped"
    assert "DIVERGED" not in line and "re-clone" not in line, line
    assert "timed out" in line and "git merge-base" in line, line
    assert f"{fsck_tracker_health._FSCK_GIT_TIMEOUT}s" in line, line
    assert is_issue is False, "a watchdog timeout is not a counted divergence issue"
    (item,) = json.loads(fsck._transform_json(line))["issues"]
    assert item["counted"] is False, item


def test_merge_base_timeout_is_reported_as_timeout_not_diverged(
    repo_with_origin: tuple[Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rebar._commands import fsck

    _repo, tracker, _seed = repo_with_origin
    assert fsck._tracker_sync_status(str(tracker)) == (None, False), "precondition: healthy"
    _time_out_git(monkeypatch, when=lambda a: "merge-base" in a and "--is-ancestor" not in a)

    _assert_timeout_report(*fsck._tracker_sync_status(str(tracker)))


def test_is_ancestor_timeout_is_reported_as_timeout_not_diverged(
    repo_with_origin: tuple[Path, Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rebar._commands import fsck

    _repo, tracker, _seed = repo_with_origin
    _time_out_git(monkeypatch, when=lambda a: "--is-ancestor" in a)

    _assert_timeout_report(*fsck._tracker_sync_status(str(tracker)))
