"""Held-out branch matrix for strict versus legacy best-effort push delivery."""

from __future__ import annotations

import contextlib
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from rebar import config
from rebar._store import compat, lock, push

pytestmark = pytest.mark.unit


def _completed(args: tuple[str, ...], rc: int = 0, out: str = "", err: str = ""):
    return subprocess.CompletedProcess(args, rc, out, err)


def _common(monkeypatch: pytest.MonkeyPatch, tracker: Path) -> None:
    tracker.mkdir(exist_ok=True)
    monkeypatch.setattr(push, "_push_mode", lambda _root=None: "always")
    monkeypatch.setattr(config, "tickets_branch", lambda _root=None: "tickets")
    monkeypatch.setattr(config, "tickets_remote", lambda _root=None: "origin")


def _delivery_error(call: Callable[[], None], reason: str) -> Exception:
    with pytest.raises(Exception) as caught:
        call()
    error = caught.value
    assert type(error).__name__ == "PushDeliveryError"
    assert getattr(error, "reason", None) == reason
    assert "unpushed commits" in str(error)
    return error


@pytest.mark.parametrize(
    ("mode", "reason"),
    [("off", "push-disabled"), ("async", "async-delivery-unobservable")],
)
def test_strict_rejects_unobservable_push_modes_while_best_effort_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, reason: str
) -> None:
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    monkeypatch.setattr(push, "_push_mode", lambda _root=None: mode)
    monkeypatch.setattr(
        push,
        "_git",
        lambda _base, *args, **_kwargs: _completed(
            args, out="2\n" if args[:2] == ("rev-list", "--count") else ""
        ),
    )
    _delivery_error(lambda: push.push_tickets_branch(str(tracker), strict=True), reason)
    assert push.push_tickets_branch(str(tracker)) is None


def test_strict_invalid_destination_and_missing_remote_fail_without_changing_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracker = tmp_path / ".tickets-tracker"
    _common(monkeypatch, tracker)

    def invalid_branch(_root=None):
        raise config.ConfigError("broken destination config")

    monkeypatch.setattr(config, "tickets_branch", invalid_branch)
    monkeypatch.setattr(
        push,
        "_git",
        lambda _base, *args, **_kwargs: _completed(
            args, out="4\n" if args[:2] == ("rev-list", "--count") else ""
        ),
    )
    error = _delivery_error(
        lambda: push.push_tickets_branch(str(tracker), strict=True), "invalid-destination"
    )
    assert "broken destination config" in str(error)
    assert push.push_tickets_branch(str(tracker)) is None

    monkeypatch.setattr(config, "tickets_branch", lambda _root=None: "tickets")

    def missing_git(_base: str, *args: str, **_kwargs: object):
        if args[:2] == ("remote", "get-url"):
            return _completed(args, 2, err="No such remote 'origin'")
        if args[:2] == ("rev-list", "--count"):
            return _completed(args, out="4\n")
        return _completed(args)

    monkeypatch.setattr(push, "_git", missing_git)
    error = _delivery_error(
        lambda: push.push_tickets_branch(str(tracker), strict=True), "remote-not-found"
    )
    assert "No such remote" in str(error)
    assert push.push_tickets_branch(str(tracker)) is None


@pytest.mark.parametrize(
    ("stderr", "reason"),
    [
        ("remote: GH013 rule violation\npre-receive hook declined", "push-policy-declined"),
        ("fatal: unable to access remote: operation timed out", "push-transport-failed"),
    ],
)
def test_strict_terminal_push_classes_have_distinct_reasons_and_preserve_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stderr: str,
    reason: str,
) -> None:
    tracker = tmp_path / ".tickets-tracker"
    _common(monkeypatch, tracker)
    witness = tracker / "local-commit.witness"
    witness.write_text("unchanged\n")
    before = witness.read_bytes()
    push_calls = 0

    def terminal_git(_base: str, *args: str, **_kwargs: object):
        nonlocal push_calls
        if args[:2] == ("remote", "get-url"):
            return _completed(args, out="local-origin\n")
        if args and args[0] == "push":
            push_calls += 1
            return _completed(args, 1, err=stderr)
        if args[:2] == ("rev-list", "--count"):
            return _completed(args, out="7\n")
        return _completed(args)

    monkeypatch.setattr(push, "_git", terminal_git)
    monkeypatch.setattr(push.sys, "exit", lambda *_a, **_k: pytest.fail("core called sys.exit"))

    error = _delivery_error(lambda: push.push_tickets_branch(str(tracker), strict=True), reason)
    assert stderr.splitlines()[0] in str(error)
    assert "7 unpushed commits" in str(error)
    assert capsys.readouterr() == ("", "")
    assert witness.read_bytes() == before
    assert push_calls == 1

    assert push.push_tickets_branch(str(tracker)) is None
    assert push_calls == 2


def _non_ff_git(
    *, dirty_merge: bool = False, push_results: list[int] | None = None
) -> tuple[Callable[..., subprocess.CompletedProcess[str]], list[tuple[str, ...]]]:
    calls: list[tuple[str, ...]] = []
    results = list(push_results or [1])

    def fake(_base: str, *args: str, **_kwargs: object):
        calls.append(args)
        if args[:2] == ("remote", "get-url"):
            return _completed(args, out="local-origin\n")
        if args and args[0] == "push":
            rc = results.pop(0) if results else 1
            return _completed(args, rc, err="rejected non-fast-forward" if rc else "")
        if args[:2] == ("rev-list", "--count"):
            return _completed(args, out="5\n")
        if args and args[0] == "merge":
            if dirty_merge:
                return _completed(args, 1, err="local changes would be overwritten by merge")
            return _completed(args)
        return _completed(args)

    return fake, calls


@contextlib.contextmanager
def _open_lock(*_args: object, **_kwargs: object) -> Iterator[None]:
    yield


def test_strict_merge_recovery_and_both_epoch_guards_have_stable_reasons(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracker = tmp_path / ".tickets-tracker"
    _common(monkeypatch, tracker)
    monkeypatch.setattr(lock, "write_lock", _open_lock)

    git, _calls = _non_ff_git()
    monkeypatch.setattr(push, "_git", git)
    monkeypatch.setattr(
        lock,
        "check_no_rebase_in_progress",
        lambda _base: (_ for _ in ()).throw(lock.RebaseGuard("merge", str(tracker))),
    )
    _delivery_error(
        lambda: push.push_tickets_branch(str(tracker), strict=True), "merge-recovery-blocked"
    )

    monkeypatch.setattr(lock, "check_no_rebase_in_progress", lambda _base: None)
    git, _calls = _non_ff_git()
    monkeypatch.setattr(push, "_git", git)
    monkeypatch.setattr(
        compat, "store_epoch_merge_target", lambda _base, _ref: (None, "epoch mismatch")
    )
    _delivery_error(
        lambda: push.push_tickets_branch(str(tracker), strict=True), "store-epoch-pre-merge"
    )

    git, _calls = _non_ff_git(dirty_merge=True)
    monkeypatch.setattr(push, "_git", git)
    epoch_checks = iter([("origin/tickets", None), (None, "epoch changed after stash")])
    monkeypatch.setattr(compat, "store_epoch_merge_target", lambda _base, _ref: next(epoch_checks))
    _delivery_error(
        lambda: push.push_tickets_branch(str(tracker), strict=True),
        "store-epoch-during-recovery",
    )


def test_strict_lock_timeout_is_typed_and_best_effort_still_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracker = tmp_path / ".tickets-tracker"
    _common(monkeypatch, tracker)

    def busy_lock(*_args: object, **_kwargs: object):
        raise lock.LockTimeout("busy writer")

    monkeypatch.setattr(lock, "write_lock", busy_lock)
    git, _calls = _non_ff_git()
    monkeypatch.setattr(push, "_git", git)
    _delivery_error(lambda: push.push_tickets_branch(str(tracker), strict=True), "lock-timeout")

    git, _calls = _non_ff_git()
    monkeypatch.setattr(push, "_git", git)
    assert push.push_tickets_branch(str(tracker)) is None


def test_five_rejections_spend_one_terminal_push_and_earlier_success_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracker = tmp_path / ".tickets-tracker"
    _common(monkeypatch, tracker)
    monkeypatch.setattr(lock, "write_lock", _open_lock)
    monkeypatch.setattr(lock, "check_no_rebase_in_progress", lambda _base: None)
    monkeypatch.setattr(
        compat, "store_epoch_merge_target", lambda _base, _ref: ("origin/tickets", None)
    )

    exhausted_git, exhausted_calls = _non_ff_git(push_results=[1, 1, 1, 1, 1, 1])
    monkeypatch.setattr(push, "_git", exhausted_git)
    _delivery_error(
        lambda: push.push_tickets_branch(str(tracker), strict=True), "final-push-rejected"
    )
    assert [call for call in exhausted_calls if call and call[0] == "push"] == [
        ("push", "origin", "HEAD:tickets")
    ] * 6

    early_git, early_calls = _non_ff_git(push_results=[1, 0])
    monkeypatch.setattr(push, "_git", early_git)
    assert push.push_tickets_branch(str(tracker), strict=True) is None
    assert len([call for call in early_calls if call and call[0] == "push"]) == 2
