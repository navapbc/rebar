"""Name possible external causes in mutation-test diagnostics (f0fb).

A detached run shares its checkout with other activity. A concurrent file,
commit, or reaped child can appear mid-test. The guard must not claim the
test caused it. Browser-probe failures report child returncode so a signal
is distinguishable from a product failure."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parents[1]
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from _child_diag import child_failure_detail  # noqa: E402
from _isolation import (  # noqa: E402
    head_move_failure_message,
    leak_failure_message,
    working_tree_failure_message,
)


def test_leak_message_does_not_assert_the_test_created_the_entry():
    """A concurrent external file can cross the leak guard's snapshot window.
    Report the entry without claiming an innocent test created it."""
    message = leak_failure_message(["EXTERNAL_MUTATION_PROBE"])

    assert "EXTERNAL_MUTATION_PROBE" in message
    # It must still tell an actually-leaking test how to fix itself.
    assert "tmp_path" in message
    # But it must NOT assert the test did it, and it MUST offer the other cause.
    assert "Test leaked new entries into REPO_ROOT" not in message
    lowered = message.lower()
    assert "concurrent" in lowered
    assert "outside" in lowered or "external" in lowered


_BEFORE = "cc3fba5cfb2222222222222222222222222222222"
_AFTER = "018dbb14621111111111111111111111111111111"


def test_head_move_message_does_not_prescribe_an_unconditional_reset():
    """A moved HEAD can come from another session. Do not prescribe bare
    git reset --hard, which could discard that commit and local changes."""
    message = head_move_failure_message(_BEFORE, _AFTER)

    reset = f"git reset --hard {_BEFORE[:10]}"
    assert reset in message, "the remedy is still worth offering to whoever owns the commit"
    # ...but only after the message has told the reader not to run it blind.
    lowered = message.lower()
    warning_at = lowered.find("do not")
    assert warning_at != -1, "no explicit do-not-reset warning"
    assert warning_at < lowered.find(reset.lower()), (
        "the reset is prescribed before the reader is warned off it"
    )


def test_head_move_message_does_not_assert_the_test_committed():
    """A sha inequality across a wall-clock window carries no attribution signal,
    so `it committed into the rebar checkout` is a claim the guard cannot make.
    `docs/local-dev-env.md` already documents this guard firing on a concurrent
    commit, rebase or branch switch by another writer."""
    message = head_move_failure_message(_BEFORE, _AFTER)

    # It must still tell an actually-leaking test how to isolate itself.
    assert "GIT_CEILING_DIRECTORIES" in message
    assert "committed into the rebar checkout instead of" not in message
    lowered = message.lower()
    assert "concurrent" in lowered
    assert "outside" in lowered or "external" in lowered


def test_child_failure_detail_names_the_signal_for_a_killed_child():
    """`kill -9` on a browser probe yielded `produced no output; stderr:` with an
    EMPTY stderr and no other information — returncode was -9 and never reported
    (4/4 deterministic). The detail must name the signal."""
    killed = subprocess.run(
        [sys.executable, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert killed.returncode == -9, killed.returncode

    detail = child_failure_detail(killed)

    assert "-9" in detail
    assert "SIGKILL" in detail
    assert "kill" in detail.lower()


def test_child_failure_detail_reports_a_plain_nonzero_exit_distinctly():
    """A crash is NOT a reap; the two must not read the same."""
    crashed = subprocess.run(
        [sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert crashed.returncode == 3

    detail = child_failure_detail(crashed)

    assert "3" in detail
    assert "SIGKILL" not in detail
    assert "signal" not in detail.lower()


def test_child_failure_detail_includes_stderr_when_the_child_wrote_any():
    noisy = subprocess.run(
        [sys.executable, "-c", "import sys; sys.stderr.write('real parse error'); sys.exit(1)"],
        capture_output=True,
        text=True,
        check=False,
    )

    detail = child_failure_detail(noisy)

    assert "real parse error" in detail


def test_working_tree_backstop_message_does_not_assert_a_test_made_the_change():
    """Two git-status snapshots cannot identify who changed a path. Concurrent
    commits, rebases, branch switches, or saves may produce the same diff.
    Report the external possibility without assigning blame."""
    message = working_tree_failure_message(["?? EXTERNAL_MUTATION_PROBE"])

    # Detection is unchanged: it still fails the run and still names every entry.
    assert "REPO ISOLATION FAILURE" in message
    assert "?? EXTERNAL_MUTATION_PROBE" in message
    # It must still tell an actually-leaking test how to fix itself.
    assert "tmp_path" in message
    # But it must NOT assert the test did it, nor presume wrongdoing...
    assert "a test wrote into the working tree instead of tmp_path" not in message
    assert "offending" not in message.lower()
    # ...and it MUST offer the cause it cannot rule out.
    lowered = message.lower()
    assert "concurrent" in lowered
    assert "outside" in lowered or "external" in lowered
