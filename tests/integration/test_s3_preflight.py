"""Fail-closed preflight when an s3:// ticket remote lacks a git-remote-s3 helper (story 8970).

An `s3://` (or `s3+zip://`) ticket-store remote is only safe with a `git-remote-s3` helper
carrying the `IfNoneMatch` per-ref lock; without it the store silently corrupts. This story
wires `rebar._optional.require_s3_helper()` into the store's push path and into init/mount so a
missing/too-old helper fails closed **at the point of misconfiguration** with the actionable
`pip install 'nava-rebar[s3]'` message, instead of later as unclonable corruption.

The guard is a deliberate fail-closed exception to `push_tickets_branch`'s best-effort contract:
it raises `OptionalDependencyError` unconditionally (independent of `strict`), because a
misconfigured S3 remote must halt loudly rather than be swallowed. These oracles drive the real
`require_s3_helper()` by monkeypatching the two seams it reads (`shutil.which`,
`importlib.metadata.version`) — proving the wiring, not a mock of it.
"""

from __future__ import annotations

import importlib.metadata
import subprocess
from pathlib import Path

import pytest

from rebar import _optional, config
from rebar._commands import init
from rebar._store import push

pytestmark = pytest.mark.integration


def _arrange_helper(monkeypatch: pytest.MonkeyPatch, *, on_path: bool, version: str | None) -> None:
    """Arrange require_s3_helper()'s two probes (the same seams test_s3_optional_guard uses)."""
    monkeypatch.setattr(
        _optional.shutil,
        "which",
        lambda name: "/usr/bin/git-remote-s3" if on_path else None,
    )

    def _version(dist: str) -> str:
        if version is None:
            raise importlib.metadata.PackageNotFoundError(dist)
        return version

    monkeypatch.setattr(_optional.importlib.metadata, "version", _version)


def _fake_git_factory(remote_url: str, *, url_rc: int = 0, push_rc: int = 0):
    """A push._git double: `remote get-url` yields remote_url; `push` yields push_rc."""

    def fake_git(_base: str, *args: str, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if args[:2] == ("remote", "get-url"):
            return subprocess.CompletedProcess(
                args,
                url_rc,
                "" if url_rc else f"{remote_url}\n",
                "no such remote" if url_rc else "",
            )
        if args and args[0] == "push":
            return subprocess.CompletedProcess(args, push_rc, "", "")
        if args[:2] == ("rev-list", "--count"):
            return subprocess.CompletedProcess(args, 0, "0\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    return fake_git


def _wire_push(
    monkeypatch: pytest.MonkeyPatch, remote_url: str, *, mode: str = "always", **kw
) -> None:
    monkeypatch.setattr(push, "_push_mode", lambda _root=None: mode)
    monkeypatch.setattr(config, "tickets_branch", lambda _root=None: "tickets")
    monkeypatch.setattr(config, "tickets_remote", lambda _root=None: "origin")
    monkeypatch.setattr(push, "_git", _fake_git_factory(remote_url, **kw))


# --------------------------------------------------------------------------------------------
# Happy path — the minimal specification of the core fail-closed behavior (implementer sees this)
# --------------------------------------------------------------------------------------------


def test_push_s3_remote_missing_helper_fails_closed(tmp_path: Path, monkeypatch) -> None:
    """s3:// remote + helper absent -> best-effort push RAISES the actionable install error.

    This is the deliberate override of push_tickets_branch's best-effort contract: even with
    strict unset (the default auto-push), a missing S3 helper must halt loudly.
    """
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    _wire_push(monkeypatch, "s3://my-bucket/tickets")
    _arrange_helper(monkeypatch, on_path=False, version="0.3.2")

    with pytest.raises(_optional.OptionalDependencyError) as ei:
        push.push_tickets_branch(str(tracker))  # best-effort (no strict)
    assert "pip install 'nava-rebar[s3]'" in str(ei.value)


# --------------------------------------------------------------------------------------------
# HELD OUT — edge / boundary / E2E (moved out of the implementer's tree)
# --------------------------------------------------------------------------------------------


def test_push_s3_helper_too_old_fails_closed(tmp_path: Path, monkeypatch) -> None:
    """s3:// remote + helper present but < 0.3.2 -> fail closed naming the >=0.3.2 minimum."""
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    _wire_push(monkeypatch, "s3://my-bucket/tickets")
    _arrange_helper(monkeypatch, on_path=True, version="0.2.9")

    with pytest.raises(_optional.OptionalDependencyError) as ei:
        push.push_tickets_branch(str(tracker))
    assert "0.3.2" in str(ei.value)


def test_push_s3zip_scheme_fails_closed(tmp_path: Path, monkeypatch) -> None:
    """s3+zip:// is also S3 mode: helper absent -> fail closed with the install message."""
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    _wire_push(monkeypatch, "s3+zip://my-bucket/tickets")
    _arrange_helper(monkeypatch, on_path=False, version="0.3.2")

    with pytest.raises(_optional.OptionalDependencyError) as ei:
        push.push_tickets_branch(str(tracker))
    assert "pip install 'nava-rebar[s3]'" in str(ei.value)


def test_push_non_s3_remote_is_noop_even_without_helper(tmp_path: Path, monkeypatch) -> None:
    """A non-s3 remote is a no-op: push proceeds and returns None with NO helper installed."""
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    _wire_push(monkeypatch, "https://github.com/org/repo.git", push_rc=0)
    _arrange_helper(monkeypatch, on_path=False, version=None)

    assert push.push_tickets_branch(str(tracker)) is None


def test_push_unresolvable_remote_keeps_best_effort_skip(tmp_path: Path, monkeypatch) -> None:
    """An unresolvable remote (get-url rc!=0) keeps the existing remote-not-found skip.

    The missing URL must NOT be misread as S3 mode: best-effort returns None (no raise), and in
    particular NOT the S3 OptionalDependencyError.
    """
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    _wire_push(monkeypatch, "s3://my-bucket/tickets", url_rc=1)
    _arrange_helper(monkeypatch, on_path=False, version=None)

    assert push.push_tickets_branch(str(tracker)) is None


@pytest.mark.allow_repo_writes  # patches module-global subprocess.Popen; opt out of HEAD guard
def test_push_async_arm_fails_closed_in_parent_before_detach(tmp_path: Path, monkeypatch) -> None:
    """async mode: the parent fails closed BEFORE detaching a child (message not swallowed)."""
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    _wire_push(monkeypatch, "s3://my-bucket/tickets", mode="async")
    _arrange_helper(monkeypatch, on_path=False, version="0.3.2")

    spawned: list[object] = []
    monkeypatch.setattr(push.subprocess, "Popen", lambda *a, **k: spawned.append((a, k)) or None)

    with pytest.raises(_optional.OptionalDependencyError) as ei:
        push.push_tickets_branch(str(tracker))
    assert "pip install 'nava-rebar[s3]'" in str(ei.value)
    assert spawned == [], "async S3 misconfig must fail in the parent, not a detached child"


def test_init_mount_s3_remote_missing_helper_fails_closed(tmp_path: Path, monkeypatch) -> None:
    """E2E: init/mount against a real repo whose configured remote is s3:// fails closed."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "--initial-branch=main", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", "s3://my-bucket/tickets"],
        check=True,
    )
    tracker = str(tmp_path / ".tickets-tracker")

    _arrange_helper(monkeypatch, on_path=False, version="0.3.2")

    with pytest.raises(_optional.OptionalDependencyError) as ei:
        init._mount_or_create_branch(str(repo), tracker)
    assert "pip install 'nava-rebar[s3]'" in str(ei.value)
