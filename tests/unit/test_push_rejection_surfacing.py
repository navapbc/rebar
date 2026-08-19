"""A tickets-branch push REJECTED BY REMOTE POLICY must surface to the writing caller
(bug 2a76 / thorough-turophilic-airedale).

The incident: GitHub push protection (GH013) rejected every `tickets` push for ~8 hours.
122 ticket commits piled up locally while every `rebar comment`/`transition`/`create`
returned normally. Found only because an unrelated CI failure prompted a manual `git push`,
which finally printed the reason.

Mechanism (proven at runtime, see the ticket's RCA comment):

* ``_NON_FF`` (push.py) matched the BARE token ``rejected``. Git prints
  ``! [remote rejected] HEAD -> tickets (pre-receive hook declined)`` for EVERY server-side
  decline -- push protection, pre-receive hook, branch protection -- not just a
  non-fast-forward. So a permanent policy rejection was misrouted onto the retriable
  non-fast-forward path, skipping the ONLY stderr-bearing log call, burning three futile
  fetch+merge cycles, and exiting with a bare ``"failed after 3 retries"`` that named neither
  the reason nor the backlog. The reason WAS captured by ``run_git`` and then discarded.
* Nothing re-announced the growing divergence: the message is byte-identical on write #1 and
  write #122, and ``PUSH_PENDING`` is computed only by an explicitly-invoked ``rebar fsck``.

The contract these tests must NOT break: push is best-effort and **never fails the caller**
(``docs/concurrency.md:296-299``; ``push_tickets_branch`` "ALWAYS returns None"). The remedy
is a richer SIGNAL, never an exception and never a non-zero exit.

Everything here drives REAL git against a REAL local bare origin whose ``pre-receive`` hook
declines the push -- no mocks, no call-count assertions (the ticket's AC says so explicitly).
The hook records each invocation to a file, so "how many times did we actually hit the
remote" is observed from the remote side rather than from a spy.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

import pytest
from _subprocess_env import subprocess_env

from rebar._store import push

pytestmark = pytest.mark.unit

# A faithful GH013 push-protection decline. The `remote:` lines are what the server prints;
# the `! [remote rejected] ... (pre-receive hook declined)` porcelain line is git's own and is
# what `_NON_FF` used to match on.
_GH013_HOOK = """\
#!/bin/sh
echo "$(date +%s%N)" >> "$REJECT_LOG"
echo "remote: error: GH013: Repository rule violations found for refs/heads/tickets." >&2
echo "remote: - Push cannot contain secrets" >&2
echo "remote:   locations:" >&2
echo "remote:     - commit: 0a09e51c9f" >&2
echo "remote:       path: 129e-2d88-cce2-492c/evidence-COMMENT.json:1" >&2
exit 1
"""


def _git(d: Path, *a: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    r = subprocess.run(["git", "-C", str(d), *a], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise AssertionError(f"git {' '.join(a)} failed: {r.stderr}")
    return r


def _bare_git(d: Path, *a: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    r = subprocess.run(["git", "--git-dir", str(d), *a], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise AssertionError(f"git {' '.join(a)} failed: {r.stderr}")
    return r


def _ident(d: Path) -> None:
    _git(d, "config", "user.email", "t@e.com")
    _git(d, "config", "user.name", "T")
    _git(d, "config", "gc.auto", "0")


@pytest.fixture
def rejecting_origin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """A tracker whose origin DECLINES every push by policy, with one unpushed local commit.

    Returns ``(tracker, reject_log)``. ``reject_log`` gains one line per real push attempt
    that reached the remote, so retry behaviour is measured from the server side.
    """
    origin = tmp_path / "origin.git"
    tracker = tmp_path / "tracker"
    reject_log = tmp_path / "reject.log"
    reject_log.touch()

    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True, capture_output=True)
    tracker.mkdir()
    _git(tracker, "init", "-q")
    _ident(tracker)
    _git(tracker, "remote", "add", "origin", str(origin))

    # Seed a shared base commit and publish it, so origin/tickets EXISTS -- the incident
    # shape (a diverging branch), not a first-push-of-a-new-branch.
    (tracker / "seed.json").write_text("{}\n")
    _git(tracker, "add", "seed.json")
    _git(tracker, "commit", "-q", "-m", "seed")
    _git(tracker, "push", "-q", "origin", "HEAD:tickets")
    _git(tracker, "fetch", "-q", "origin", "+refs/heads/tickets:refs/remotes/origin/tickets")

    # Now arm the policy decline.
    hook = origin / "hooks" / "pre-receive"
    hook.write_text(_GH013_HOOK)
    hook.chmod(0o755)
    _bare_git(origin, "config", "core.hooksPath", str(origin / "hooks"))

    # One local-only commit: the write whose push will be rejected.
    (tracker / "evidence.json").write_text('{"body": "evidence recorded during the outage"}\n')
    _git(tracker, "add", "evidence.json")
    _git(tracker, "commit", "-q", "-m", "ticket: COMMENT evidence")

    monkeypatch.setenv("REBAR_SYNC_PUSH", "always")
    monkeypatch.setenv("REJECT_LOG", str(reject_log))
    return tracker, reject_log


def _ahead(tracker: Path) -> int:
    out = _git(tracker, "rev-list", "origin/tickets..HEAD", "--count").stdout.strip()
    return int(out or "0")


def test_policy_rejection_surfaces_the_git_reason_to_the_caller(
    rejecting_origin: tuple[Path, Path], caplog: pytest.LogCaptureFixture
) -> None:
    """The warning must name WHY the remote refused, not just that a retry count elapsed.

    This is the whole defect: `run_git` captures the GH013 body and the offending path, and
    the terminal message throws them away, so an eight-hour permanent outage reads exactly
    like transient contention.
    """
    tracker, _log = rejecting_origin
    assert _ahead(tracker) == 1, "fixture did not reach the local-ahead state"

    with caplog.at_level(logging.WARNING, logger="rebar._store.push"):
        push.push_tickets_branch(str(tracker))

    text = caplog.text
    assert text.strip(), "a rejected push produced NO warning at all"
    assert "GH013" in text or "pre-receive hook declined" in text or "declined" in text, (
        "the push failure warning does not name the git rejection reason; the caller cannot "
        f"tell a permanent policy rejection from transient contention. Got:\n{text}"
    )


def test_policy_rejection_surfaces_the_unpushed_backlog(
    rejecting_origin: tuple[Path, Path], caplog: pytest.LogCaptureFixture
) -> None:
    """The warning must name HOW MANY commits are now unpushed.

    Without this the message is byte-identical on write #1 and write #122, so a rejection
    that scrolled past is unrecoverable context -- the mechanism that let the real incident
    run for eight hours.
    """
    tracker, _log = rejecting_origin

    # Two more local writes, so the backlog is unambiguously 3 (not 0 or 1, which could be
    # matched by an incidental digit in a path or a retry count).
    for n in range(2):
        (tracker / f"more{n}.json").write_text("{}\n")
        _git(tracker, "add", f"more{n}.json")
        _git(tracker, "commit", "-q", "-m", f"ticket: COMMENT more{n}")
    assert _ahead(tracker) == 3

    with caplog.at_level(logging.WARNING, logger="rebar._store.push"):
        push.push_tickets_branch(str(tracker))

    text = caplog.text
    assert "3" in text, (
        "the push failure warning does not report the number of unpushed commits, so "
        f"successive failures are indistinguishable and the backlog never escalates. Got:\n{text}"
    )
    assert "unpushed" in text.lower() or "ahead" in text.lower() or "pending" in text.lower(), (
        f"the backlog count is not labelled, so '3' is not readable as a commit count:\n{text}"
    )


def test_policy_rejection_is_not_retried_as_a_non_fast_forward(
    rejecting_origin: tuple[Path, Path], caplog: pytest.LogCaptureFixture
) -> None:
    """A rule violation is permanent; fetch+merge+retry cannot fix it.

    Measured from the REMOTE side (the hook logs each invocation), not from a spy on our own
    code -- so this asserts observable behaviour against the server, per the ticket's AC.
    """
    tracker, reject_log = rejecting_origin

    with caplog.at_level(logging.WARNING, logger="rebar._store.push"):
        push.push_tickets_branch(str(tracker))

    attempts = len([ln for ln in reject_log.read_text().splitlines() if ln.strip()])
    assert attempts == 1, (
        f"a permanent policy rejection hit the remote {attempts} times; it is misclassified "
        "as a retriable non-fast-forward and burns the whole retry budget on futile "
        "fetch+merge cycles that cannot resolve a rule violation"
    )


def test_rejection_never_fails_the_caller_and_keeps_the_local_commit(
    rejecting_origin: tuple[Path, Path], caplog: pytest.LogCaptureFixture
) -> None:
    """The best-effort contract is preserved (docs/concurrency.md:296-299).

    Guards the OPPOSITE failure: a fix that surfaces the rejection by raising, or by
    discarding the local commit, would be a regression -- "never fails the caller" is
    documented intent, pinned by push.py's "ALWAYS returns None" and tests/unit/test_store.py.
    """
    tracker, _log = rejecting_origin
    head_before = _git(tracker, "rev-parse", "HEAD").stdout.strip()

    with caplog.at_level(logging.WARNING, logger="rebar._store.push"):
        result = push.push_tickets_branch(str(tracker))  # must not raise

    assert result is None, "push_tickets_branch must keep returning None (best-effort contract)"
    assert _git(tracker, "rev-parse", "HEAD").stdout.strip() == head_before, (
        "the rejected push moved or lost the local commit; a failed push must leave local "
        "commits intact"
    )
    assert _ahead(tracker) == 1, "the local-ahead commit must survive a rejected push"


def test_strict_module_cli_translates_delivery_error_without_writing_an_event(
    rejecting_origin: tuple[Path, Path],
) -> None:
    """Only the module boundary converts strict failure to stderr and nonzero."""
    tracker, _log = rejecting_origin
    head_before = _git(tracker, "rev-parse", "HEAD").stdout.strip()
    events_before = {
        path.relative_to(tracker): path.read_bytes() for path in tracker.rglob("*.json")
    }

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "rebar._store.push",
            "push",
            "--tracker",
            str(tracker),
            "--strict",
        ],
        cwd=tracker,
        env=subprocess_env(),
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    assert completed.stdout == ""
    assert "push-policy-declined" in completed.stderr
    assert "GH013" in completed.stderr or "pre-receive hook declined" in completed.stderr
    assert "1 unpushed commits on origin/tickets..HEAD" in completed.stderr
    assert _git(tracker, "rev-parse", "HEAD").stdout.strip() == head_before
    assert _ahead(tracker) == 1
    events_after = {
        path.relative_to(tracker): path.read_bytes() for path in tracker.rglob("*.json")
    }
    assert events_after == events_before
    assert not any("BRIDGE_ALERT" in path.name for path in tracker.rglob("*"))
