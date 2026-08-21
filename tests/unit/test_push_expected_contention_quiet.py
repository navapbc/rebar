"""EXPECTED, self-healing tickets-push outcomes must be suppressed from
agent-visible output (bug 3ff9-a8f0-ff5a-457f / squeamish-halfawake-fantail).

Bug 2a76 made the terminal push-failure report informative; the operator ruling of
2026-08-21 draws the line the other way for outcomes that are expected and handled
automatically: a lost contention race under concurrent writers, and a transient
transport fault the code is already retrying, heal on the next successful write —
surfacing them at WARNING primed agent sessions to assume the store was broken and
burn tokens investigating ref topology. The ruling update of the same day goes
further: "We should suppress the message under normal load. This is noise, not an
outage signal." — so the expected case emits NOTHING at INFO or above (DEBUG at most).

The distinction these tests pin:

* expected-and-self-healing (lost contention race; mid-retry transport blip) → DEBUG,
  wording that states the contract affirmatively ("expected under concurrent …
  no action needed") — and NO record at INFO or above;
* operator-actionable stays loud — a policy decline, a strict raise, and a backlog
  that GREW across successive failures (the durable push-pending marker records the
  previous count, so growth is provable without new state);
* no message may say bare ``..HEAD`` — the tracker's HEAD reads as the session's code
  worktree HEAD, which is exactly the ambiguity that burned a session turn.

The scaffold mirrors ``test_push_strict.py``: a monkeypatched ``push._git`` drives the
loop deterministically; the marker/observability assertions run against the real
tracker directory so the durable channel is proven intact.
"""

from __future__ import annotations

import contextlib
import logging
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from rebar import config
from rebar._store import compat, lock, push, push_recovery, push_state

pytestmark = pytest.mark.unit

_REJECTED = (
    "To local-origin\n"
    " ! [rejected]        HEAD -> tickets (fetch first)\n"
    "error: failed to push some refs to 'local-origin'\n"
    "hint: Updates were rejected because the remote contains work that you do not have"
)

_TLS_BLIP = (
    "fatal: unable to access 'https://github.com/navapbc/rebar/': "
    "server certificate verification failed. CAfile: none CRLfile: none"
)


def _completed(args: tuple[str, ...], rc: int = 0, out: str = "", err: str = ""):
    return subprocess.CompletedProcess(args, rc, out, err)


@contextlib.contextmanager
def _open_lock(*_args: object, **_kwargs: object) -> Iterator[None]:
    yield


def _contention_git(count: str) -> Callable[..., subprocess.CompletedProcess[str]]:
    """Every push loses the CAS race; recovery fetch+merge always succeeds cleanly."""

    def fake(_base: str, *args: str, **_kwargs: object):
        if args[:2] == ("remote", "get-url"):
            return _completed(args, out="local-origin\n")
        if args and args[0] == "push":
            return _completed(args, 1, err=_REJECTED)
        if args[:2] == ("rev-list", "--count"):
            return _completed(args, out=f"{count}\n")
        return _completed(args)

    return fake


def _arm(monkeypatch: pytest.MonkeyPatch, tracker: Path) -> None:
    tracker.mkdir(exist_ok=True)
    # A real git-dir location so push_state's durable marker can be written and read.
    (tracker / ".git").mkdir(exist_ok=True)
    monkeypatch.setattr(push, "_push_mode", lambda _root=None: "always")
    monkeypatch.setattr(config, "tickets_branch", lambda _root=None: "tickets")
    monkeypatch.setattr(config, "tickets_remote", lambda _root=None: "origin")
    monkeypatch.setattr(lock, "write_lock", _open_lock)
    monkeypatch.setattr(lock, "check_no_rebase_in_progress", lambda _base: None)
    monkeypatch.setattr(
        compat, "store_epoch_merge_target", lambda _base, _ref: ("origin/tickets", None)
    )


def _push_best_effort(tracker: Path, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG, logger="rebar._store.push"):
        assert push.push_tickets_branch(str(tracker), sleep_fn=lambda _d: None) is None


def test_lost_contention_race_emits_nothing_at_info_and_states_the_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """AC1 + AC3: a first lost contention race is DEBUG at most, self-healing wording."""
    tracker = tmp_path / ".tickets-tracker"
    _arm(monkeypatch, tracker)
    monkeypatch.setattr(push, "_git", _contention_git("5"))

    _push_best_effort(tracker, caplog)

    loud = [r for r in caplog.records if r.levelno >= logging.INFO]
    assert loud == [], (
        "a lost contention race is expected and self-healing NOISE under normal load "
        "(operator ruling: not an outage signal); anything at INFO or above primes agent "
        f"sessions to investigate a non-issue. Got: {[r.getMessage() for r in loud]}"
    )
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert text.strip(), "the outcome must still be traceable (at DEBUG), not silenced entirely"
    assert "..HEAD" not in text, (
        "bare ..HEAD reads as the session's code worktree HEAD; name the tickets branch:\n" + text
    )
    assert "tickets branch" in text, f"the backlog message must name the tickets branch:\n{text}"
    assert "next successful push" in text, (
        f"the message must state the self-healing contract (publishes on next push):\n{text}"
    )
    assert "expected under concurrent" in text and "no action needed" in text, (
        f"the message must state affirmatively that no action is needed:\n{text}"
    )


def test_lost_contention_race_still_records_the_durable_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """AC1: quieting the log must not quiet the STATE channel (marker/fsck/push_status)."""
    tracker = tmp_path / ".tickets-tracker"
    _arm(monkeypatch, tracker)
    monkeypatch.setattr(push, "_git", _contention_git("5"))

    _push_best_effort(tracker, caplog)

    status = push_state.read_status(str(tracker))
    assert status.get("state") == "pending", "the durable push-pending marker must still record"
    assert status.get("reason") == "final-push-rejected"
    assert status.get("unpushed") == "5"


def test_static_backlog_across_failures_stays_quiet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A repeat failure whose backlog did NOT grow is still the expected case."""
    tracker = tmp_path / ".tickets-tracker"
    _arm(monkeypatch, tracker)
    monkeypatch.setattr(push, "_git", _contention_git("5"))
    push_state.record_failure(str(tracker), "final-push-rejected", "prior", "origin/tickets")
    assert push_state.read_status(str(tracker)).get("unpushed") == "5"

    _push_best_effort(tracker, caplog)

    loud = [r for r in caplog.records if r.levelno >= logging.INFO]
    assert loud == [], (
        "an unchanged backlog is not growth; only a GROWING backlog is operator-actionable. "
        f"Got: {[r.getMessage() for r in loud]}"
    )


def test_growing_backlog_across_failures_escalates_to_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The sanctioned loud case: the marker recorded a smaller count than today's."""
    tracker = tmp_path / ".tickets-tracker"
    _arm(monkeypatch, tracker)
    monkeypatch.setattr(push, "_git", _contention_git("2"))
    push_state.record_failure(str(tracker), "final-push-rejected", "prior", "origin/tickets")
    assert push_state.read_status(str(tracker)).get("unpushed") == "2"

    monkeypatch.setattr(push, "_git", _contention_git("5"))
    _push_best_effort(tracker, caplog)

    loud = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert loud, (
        "a backlog that GREW across successive failures is the persistent-outage signal "
        "bug 2a76 kept loud; it must stay WARNING"
    )
    assert push_state.read_status(str(tracker)).get("unpushed") == "5"


def test_backlog_growth_check_reads_the_marker_before_it_is_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """backlog_grew is a pre-record read; ambiguity resolves to False (not proven)."""
    tracker = tmp_path / ".tickets-tracker"
    _arm(monkeypatch, tracker)
    monkeypatch.setattr(push, "_git", _contention_git("5"))

    # No prior marker → not proven growing.
    assert push_state.backlog_grew(str(tracker), "origin/tickets") is False
    # A prior marker with a non-numeric count (corrupt/unknown) → not proven growing.
    push_state.record_failure(str(tracker), "final-push-rejected", "prior", "origin/tickets")
    marker = Path(push_state._marker_path(str(tracker)))
    marker.write_text(marker.read_text().replace('"5"', '"unknown"'))
    assert push_state.backlog_grew(str(tracker), "origin/tickets") is False


def test_mid_retry_transport_notices_in_recovery_legs_are_debug(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """AC2: the fetch-leg and merge-leg retry announcements are DEBUG, below INFO."""
    tracker = tmp_path / ".tickets-tracker"
    _arm(monkeypatch, tracker)
    flaked: dict[str, int] = {"fetch": 0, "merge": 0}

    def flaky(_base: str, *args: str, **_kwargs: object):
        if args and args[0] in ("fetch", "merge"):
            flaked[args[0]] += 1
            if flaked[args[0]] == 1:
                return _completed(args, 1, err=_TLS_BLIP)
        return _completed(args)

    monkeypatch.setattr(push, "_git", flaky)

    with caplog.at_level(logging.DEBUG, logger="rebar._store.push"):
        fetch = push_recovery._fetch_for_recovery(
            push, str(tracker), "origin", "tickets", lambda _d: None
        )
        merge = push_recovery._merge_with_transport_retry(
            push, str(tracker), "origin/tickets", "origin/tickets", lambda _d: None
        )

    assert fetch.returncode == 0 and merge.returncode == 0, "the blip must still be ridden out"
    notices = [r for r in caplog.records if "transient transport fault" in r.getMessage()]
    assert len(notices) == 2, "each leg announces its automatic retry exactly once"
    assert all(r.levelno == logging.DEBUG for r in notices), (
        "an already-being-retried transport blip is suppressed-under-normal-load noise, "
        f"DEBUG at most: {[(r.levelname, r.getMessage()) for r in notices]}"
    )
    assert all("no action needed" in r.getMessage() for r in notices)


def test_strict_caller_still_raises_with_the_reworded_backlog_suffix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC4: strict delivery still raises; its message carries the new branch-named suffix."""
    tracker = tmp_path / ".tickets-tracker"
    _arm(monkeypatch, tracker)
    monkeypatch.setattr(push, "_git", _contention_git("5"))

    with pytest.raises(Exception) as caught:
        push.push_tickets_branch(str(tracker), strict=True, sleep_fn=lambda _d: None)
    error = caught.value
    assert type(error).__name__ == "PushDeliveryError"
    assert getattr(error, "reason", None) == "final-push-rejected"
    assert "..HEAD" not in str(error)
    assert "5 unpushed commits" in str(error)
    assert "tickets branch" in str(error)
