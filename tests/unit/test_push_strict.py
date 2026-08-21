"""Held-out branch matrix for strict versus legacy best-effort push delivery."""

from __future__ import annotations

import contextlib
import logging
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from rebar import config
from rebar._store import compat, lock, push, push_classify

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
    ("stderr", "reason", "attempts"),
    [
        ("remote: GH013 rule violation\npre-receive hook declined", "push-policy-declined", 1),
        (
            "fatal: unable to access remote: operation timed out",
            "push-transport-failed",
            push_classify._MAX_TRANSPORT_ATTEMPTS,
        ),
    ],
)
def test_strict_terminal_push_classes_have_distinct_reasons_and_preserve_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    stderr: str,
    reason: str,
    attempts: int,
) -> None:
    """Both classes stay terminal with their own reason — but bug f61c gives a TRANSPORT
    fault a bounded retry first, while a policy decline is still spent in ONE attempt."""
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
    caplog.set_level("DEBUG")

    error = _delivery_error(
        lambda: push.push_tickets_branch(str(tracker), strict=True, sleep_fn=lambda _d: None),
        reason,
    )
    assert stderr.splitlines()[0] in str(error)
    assert "7 unpushed commits" in str(error)
    captured = capsys.readouterr()
    if attempts == 1:
        # The strict path stays silent: _raise_if_strict raises BEFORE the terminal
        # logger.warning, so a strict caller gets the exception and nothing else.
        assert captured == ("", "")
    # Bug f61c: a transport fault now RETRIES first, and each retry announces itself before
    # we know the attempt will end terminally — that notice is the only in-run signal that
    # delivery is degraded, so the strict path is deliberately no longer silent here.
    # Whether it reaches the captured streams or only the log handler depends on handler
    # config, so the count is asserted on caplog, which is deterministic either way.
    # Bug 3ff9: the retry is automatic — suppressed under normal load, DEBUG at most.
    retries = [r for r in caplog.records if "transient transport fault" in r.getMessage()]
    assert len(retries) == attempts - 1
    assert all(r.levelno == logging.DEBUG for r in retries), (
        "an automatic mid-retry transport notice is noise, not an outage signal — DEBUG: "
        f"{[(r.levelname, r.getMessage()) for r in retries]}"
    )
    assert witness.read_bytes() == before
    assert push_calls == attempts

    assert push.push_tickets_branch(str(tracker), sleep_fn=lambda _d: None) is None
    assert push_calls == attempts * 2


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
        lambda: push.push_tickets_branch(str(tracker), strict=True, sleep_fn=lambda _d: None),
        "final-push-rejected",
    )
    assert [call for call in exhausted_calls if call and call[0] == "push"] == [
        ("push", "origin", "HEAD:tickets")
    ] * 6

    early_git, early_calls = _non_ff_git(push_results=[1, 0])
    monkeypatch.setattr(push, "_git", early_git)
    assert push.push_tickets_branch(str(tracker), strict=True, sleep_fn=lambda _d: None) is None
    assert len([call for call in early_calls if call and call[0] == "push"]) == 2


_CI_TLS_STDERR = (
    "fatal: unable to access 'https://github.com/navapbc/rebar/': "
    "server certificate verification failed. CAfile: none CRLfile: none"
)


def test_transient_tls_failure_is_retried_and_then_delivers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bug f61c (subzero-locustal-junebug): a CONVERGED pass died on this exact stderr.

    The commits were left on the runner and died with it. One blip must not cost the pass.
    """
    tracker = tmp_path / ".tickets-tracker"
    _common(monkeypatch, tracker)
    push_calls = 0
    slept: list[float] = []

    def flaky_git(_base: str, *args: str, **_kwargs: object):
        nonlocal push_calls
        if args[:2] == ("remote", "get-url"):
            return _completed(args, out="local-origin\n")
        if args and args[0] == "push":
            push_calls += 1
            if push_calls == 1:
                return _completed(args, 1, err=_CI_TLS_STDERR)
            return _completed(args)
        if args[:2] == ("rev-list", "--count"):
            return _completed(args, out="3\n")
        return _completed(args)

    monkeypatch.setattr(push, "_git", flaky_git)

    assert push.push_tickets_branch(str(tracker), strict=True, sleep_fn=slept.append) is None
    assert push_calls == 2
    assert slept == [push_classify._TRANSPORT_BACKOFF_SECONDS[0]]


def test_persistent_transport_failure_stops_on_a_bounded_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The retry rides out a blip; it must not sit on a real outage forever."""
    tracker = tmp_path / ".tickets-tracker"
    _common(monkeypatch, tracker)
    push_calls = 0
    slept: list[float] = []

    def dead_git(_base: str, *args: str, **_kwargs: object):
        nonlocal push_calls
        if args[:2] == ("remote", "get-url"):
            return _completed(args, out="local-origin\n")
        if args and args[0] == "push":
            push_calls += 1
            return _completed(args, 1, err=_CI_TLS_STDERR)
        if args[:2] == ("rev-list", "--count"):
            return _completed(args, out="3\n")
        return _completed(args)

    monkeypatch.setattr(push, "_git", dead_git)

    error = _delivery_error(
        lambda: push.push_tickets_branch(str(tracker), strict=True, sleep_fn=slept.append),
        "push-transport-failed",
    )
    assert "server certificate verification failed" in str(error)
    assert "3 unpushed commits" in str(error)
    assert push_calls == push_classify._MAX_TRANSPORT_ATTEMPTS
    # Backoff is spent BETWEEN attempts, and it escalates rather than firing back-to-back.
    expected = push_classify._TRANSPORT_BACKOFF_SECONDS[: push_classify._MAX_TRANSPORT_ATTEMPTS - 1]
    assert slept == list(expected)
    assert slept == sorted(slept)
    assert all(delay > 0 for delay in slept)


@pytest.mark.parametrize(
    ("stderr", "retriable"),
    [
        (_CI_TLS_STDERR, True),
        ("fatal: could not fetch 983800f2 from promisor remote", True),
        ("git timed out after 30s", True),
        ("ssl certificate problem: unable to get local issuer certificate", True),
        ("fatal: could not resolve host: github.com", True),
        # A policy decline is PERMANENT — it must never become transport-retriable, even
        # when it also carries a transport-shaped phrase (bug 2a76's subtractive contract).
        ("remote: GH013 rule violation\npre-receive hook declined", False),
        ("remote: Internal Server Error", False),
        ("fatal: unable to access remote: rate limit exceeded", False),
        ("! [remote rejected] HEAD -> tickets (non-fast-forward)", False),
    ],
)
def test_transport_classifier_is_subtractive(stderr: str, retriable: bool) -> None:
    assert push_classify._is_transport_retriable(stderr) is retriable


_CANNOT_LOCK_REF_STDERR = (
    "! [remote rejected] HEAD -> tickets (cannot lock ref 'refs/heads/tickets': "
    "is at e45f61a9ef9f8a570e257079e51c9f39fa061240 but "
    "expected 9ecaaa40a28e992b060da61ef5969d425f94d1fe)"
)


def test_lost_cas_race_backs_off_between_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bug ebee (freeborn-dizzy-raven): 5 retries all lost the SAME race, back-to-back.

    `cannot lock ref ... is at <a> but expected <b>` is correctly classified retriable, so
    the budget was spent — but with no pause the retries kept colliding with the same
    concurrent tickets writer and 6 commits were left unpushed. The attempt COUNT is
    unchanged; only the spacing is.
    """
    tracker = tmp_path / ".tickets-tracker"
    _common(monkeypatch, tracker)
    monkeypatch.setattr(lock, "write_lock", _open_lock)
    monkeypatch.setattr(lock, "check_no_rebase_in_progress", lambda _base: None)
    monkeypatch.setattr(
        compat, "store_epoch_merge_target", lambda _base, _ref: ("origin/tickets", None)
    )
    slept: list[float] = []
    calls: list[tuple[str, ...]] = []
    results = [1, 1, 0]

    def racing_git(_base: str, *args: str, **_kwargs: object):
        calls.append(args)
        if args[:2] == ("remote", "get-url"):
            return _completed(args, out="local-origin\n")
        if args and args[0] == "push":
            rc = results.pop(0) if results else 1
            return _completed(args, rc, err=_CANNOT_LOCK_REF_STDERR if rc else "")
        if args[:2] == ("rev-list", "--count"):
            return _completed(args, out="6\n")
        return _completed(args)

    monkeypatch.setattr(push, "_git", racing_git)

    assert push.push_tickets_branch(str(tracker), strict=True, sleep_fn=slept.append) is None
    assert len([call for call in calls if call and call[0] == "push"]) == 3
    # One backoff per recovery that preceded a re-push, escalating rather than back-to-back.
    assert slept == list(push_classify._CAS_BACKOFF_SECONDS[:2])
    assert slept == sorted(slept)
    assert all(delay > 0 for delay in slept)


_PROMISOR_TLS_STDERR = (
    "fatal: unable to access 'https://github.com/navapbc/rebar/': "
    "server certificate verification failed. CAfile: none CRLfile: none\n"
    "fatal: could not fetch 983800f21c7d9e261b84dc340555e315eb51437a from promisor remote"
)


def _recovery_git(
    *, merge_results: list[tuple[int, str]], push_results: list[int]
) -> tuple[Callable[..., subprocess.CompletedProcess[str]], list[tuple[str, ...]]]:
    """A git whose recovery MERGE can fail with caller-chosen stderr."""
    calls: list[tuple[str, ...]] = []
    merges = list(merge_results)
    pushes = list(push_results)

    def fake(_base: str, *args: str, **_kwargs: object):
        calls.append(args)
        if args[:2] == ("remote", "get-url"):
            return _completed(args, out="local-origin\n")
        if args and args[0] == "push":
            rc = pushes.pop(0) if pushes else 1
            return _completed(args, rc, err="rejected non-fast-forward" if rc else "")
        if args[:2] == ("rev-list", "--count"):
            return _completed(args, out="2\n")
        if args and args[0] == "merge":
            if len(args) > 1 and args[1] == "--abort":
                return _completed(args)
            rc, err = merges.pop(0) if merges else (0, "")
            return _completed(args, rc, err=err)
        return _completed(args)

    return fake, calls


def _recovery_common(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lock, "write_lock", _open_lock)
    monkeypatch.setattr(lock, "check_no_rebase_in_progress", lambda _base: None)
    monkeypatch.setattr(
        compat, "store_epoch_merge_target", lambda _base, _ref: ("origin/tickets", None)
    )


def test_merge_recovery_retries_a_promisor_transport_fault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bug f61c (subzero-locustal-junebug): the TLS fault fired INSIDE `git merge`.

    The checkout is a blob:none partial clone, so the merge does on-demand promisor
    fetches — the failure surfaced as `merge-recovery-blocked` plus `could not fetch ...
    from promisor remote`, and the converged pass was abandoned with 2 unpushed commits.
    """
    tracker = tmp_path / ".tickets-tracker"
    _common(monkeypatch, tracker)
    _recovery_common(monkeypatch)
    slept: list[float] = []
    git, calls = _recovery_git(
        merge_results=[(1, _PROMISOR_TLS_STDERR), (0, "")], push_results=[1, 0]
    )
    monkeypatch.setattr(push, "_git", git)

    assert push.push_tickets_branch(str(tracker), strict=True, sleep_fn=slept.append) is None
    merges = [c for c in calls if c and c[0] == "merge" and c[1:2] != ("--abort",)]
    assert len(merges) == 2, "the transport-faulted merge must be retried"
    assert slept[0] == push_classify._TRANSPORT_BACKOFF_SECONDS[0]


def test_merge_recovery_keeps_a_genuine_conflict_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real merge conflict is NOT transport-retriable — it must still stop at once."""
    tracker = tmp_path / ".tickets-tracker"
    _common(monkeypatch, tracker)
    _recovery_common(monkeypatch)
    git, calls = _recovery_git(
        merge_results=[(1, "CONFLICT (content): Merge conflict in events.jsonl")],
        push_results=[1, 1, 1, 1, 1, 1],
    )
    monkeypatch.setattr(push, "_git", git)

    _delivery_error(
        lambda: push.push_tickets_branch(str(tracker), strict=True, sleep_fn=lambda _d: None),
        "merge-recovery-blocked",
    )
    merges = [c for c in calls if c and c[0] == "merge" and c[1:2] != ("--abort",)]
    assert len(merges) == 1, "a genuine conflict must not be retried as a transport fault"
