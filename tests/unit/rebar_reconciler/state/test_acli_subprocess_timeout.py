"""Bug d843 — _run_acli subprocess timeout + process-group reaping.

These tests exercise the load-bearing fix: a hung ``acli`` child (or a
pipe-holding grandchild) must be reaped within a bounded wall-clock budget
rather than freezing a reconcile pass, and a timed-out WRITE must NOT be
blind-retried (Jira is non-idempotent) while a READ may.

The fakes are tiny ``python -c`` programs invoked as the ``acli`` binary via
``acli_cmd=[sys.executable, "-c", ...]``. The POSIX-specific process-group
tests are skipped on non-POSIX (no ``os.killpg``).
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import NamedTuple

import pytest

# The reconciler engine is on sys.path via the package conftest; import flat.
from rebar_reconciler.adapters.jira import acli as acli_mod
from rebar_reconciler.adapters.jira import acli_cli_ops, acli_subprocess

POSIX_ONLY = pytest.mark.skipif(os.name != "posix", reason="process-group reaping is POSIX-only")


# ---------------------------------------------------------------------------
# Test-only timing windows
# ---------------------------------------------------------------------------
# These drive the existing module seams ``_acli_call_timeout`` /
# ``_ACLI_GRACE_SECONDS`` / ``_ACLI_DRAIN_SECONDS``. The production defaults
# (120s / 3s / 2s) are untouched — nothing here changes shipped behavior.
#
# Direction matters. A reap test needs its fake child to still be HANGING when the
# deadline fires, and every hanging fake sleeps for an hour, so a SMALLER window
# makes the timeout MORE certain, never less: no amount of host load can let the
# child finish first. What a small window could break is ORDERING — three reap
# tests assert on something the child must have completed first (a grandchild
# pidfile, a spawn counter, flushed partial stdout). Rather than pick a window big
# enough to out-race interpreter startup (a wall-clock bet a loaded runner
# eventually loses), :func:`real_child` makes the parent WAIT for the child to
# signal readiness, so the injected window is spent purely on the hang. That is
# what makes these values load-independent rather than merely small.
_REAP_CALL_TIMEOUT = 0.2
_REAP_GRACE_SECONDS = 0.1
_REAP_DRAIN_SECONDS = 0.1

# The opposite class: a fake child that must COMPLETE. Here the timeout is a
# hang-guard, not a pass condition — a healthy run finishes in milliseconds and
# never approaches it, so it costs no wall time AND cannot be lost to a slow host.
# (The module used to impose an autouse 1s ceiling on these too, which was a latent
# load flake in exactly that direction.)
_COMPLETING_CHILD_TIMEOUT = 30.0

# Ceilings for the two poll loops below. Deliberately generous: they exist so a
# broken spawn fails loudly instead of hanging the suite, never as a pass
# condition, so raising them can only trade a hang for a clearer failure.
_POLL_CEILING_SECONDS = 30.0
_POLL_INTERVAL_SECONDS = 0.005


def _wait_until_gone(pids) -> set[int]:
    """Poll until none of *pids* exists; return whichever survived the ceiling.

    An existence probe (``kill(pid, 0)``) polled to a transition, rather than a
    single check after a sleep: the group kill is asynchronous w.r.t. the children
    actually dying, so the only correct question is "has it happened yet?".
    """
    remaining = set(pids)
    deadline = time.monotonic() + _POLL_CEILING_SECONDS
    while remaining and time.monotonic() < deadline:
        for pid in tuple(remaining):
            try:
                os.kill(pid, 0)  # 0 == existence probe
            except ProcessLookupError:
                remaining.discard(pid)
        if remaining:
            time.sleep(_POLL_INTERVAL_SECONDS)
    return remaining


# ---------------------------------------------------------------------------
# Fake-binary programs (run as the `acli` executable)
# ---------------------------------------------------------------------------

# Every hanging fake finishes its setup by creating the file named as its LAST
# argv element. :func:`real_child` polls for that file before handing the process
# back to ``_run_acli`` — which measures its deadline from ``communicate()``, i.e.
# AFTER the wrapper returns. So "the child finished setting up" is an observed
# event, never an inference from elapsed time.
_SIGNAL_READY = """
def _ready():
    import sys
    open(sys.argv[-1], "w").close()
"""

# Forks a grandchild that inherits the stdout PIPE and hangs forever, writes the
# grandchild PID to a pidfile, then the child itself hangs holding the pipe. This
# is the exact gotcha-1 shape: subprocess.run(timeout=) would orphan the
# grandchild; only a process-GROUP kill reaps it.
_GRANDCHILD_HANG = (
    _SIGNAL_READY
    + r"""
import os, sys, time
pidfile = sys.argv[1]
pid = os.fork()
if pid == 0:
    # grandchild: keep the inherited stdout pipe open and hang
    with open(pidfile, "w") as f:
        f.write(str(os.getpid()))
    _ready()  # signal only once the pidfile is complete
    time.sleep(3600)
    os._exit(0)
# parent (the direct child): also hang, holding the pipe
time.sleep(3600)
"""
)

# A simple child that just hangs (no grandchild) — used for retry/spawn-count tests.
_SIMPLE_HANG = (
    _SIGNAL_READY
    + r"""
import time
_ready()
time.sleep(3600)
"""
)

# Appends a marker per invocation so we can count spawns, then hangs.
_COUNT_THEN_HANG = (
    _SIGNAL_READY
    + r"""
import sys, time
with open(sys.argv[1], "a") as f:
    f.write("x")
_ready()  # signal only once the spawn marker is on disk
time.sleep(3600)
"""
)

# Emits a truncated multibyte UTF-8 lead byte on stdout then hangs. With
# errors='strict' the cleanup-path decode would raise UnicodeDecodeError; with
# errors='replace' it must not.
_TRUNCATED_UTF8_THEN_HANG = (
    _SIGNAL_READY
    + r"""
import sys, time
sys.stdout.buffer.write(b"ok-\xe2\x82")
sys.stdout.buffer.flush()
_ready()  # signal only once the truncated lead byte is in the pipe
time.sleep(3600)
"""
)

# Emits partial stdout then hangs — to assert partial capture on timeout.
_PARTIAL_THEN_HANG = (
    _SIGNAL_READY
    + r"""
import sys, time
sys.stdout.write("partial-output-here")
sys.stdout.flush()
_ready()  # signal only once the partial output is in the pipe
time.sleep(3600)
"""
)

# A fast no-op that prints valid JSON and exits 0 — used by the classification
# guard so client methods complete without timing out.
_FAST_OK = r"""
import sys
sys.stdout.write("[]")
"""


def _fake_cmd(program: str, *args: str) -> list[str]:
    """Build an acli_cmd that runs *program* as the fake binary."""
    return [sys.executable, "-c", program, *args]


class _RealChild(NamedTuple):
    """Handles for a test that drives real hanging children through ``_run_acli``."""

    ready: Path
    """Pass as the LAST argv element of every fake; the child creates it when set up."""

    pids: list[int]
    """Parent-side PID of every child actually spawned, in spawn order."""

    backoffs: list[float]
    """Logical retry delays, recorded rather than slept."""


@pytest.fixture
def real_child(tmp_path, monkeypatch) -> _RealChild:
    """Drive real hanging children on sub-second windows, ordered by readiness.

    Installs the small reap windows, records every spawned PID and every logical
    retry backoff (without sleeping it), and — the load-bearing part — blocks
    inside ``Popen`` until the child signals that its setup is done. ``_run_acli``
    starts its deadline at ``communicate()``, after this wrapper returns, so the
    injected window covers the hang and nothing else.

    Nothing here asserts an elapsed time. The wait polls for an observable file and
    carries a ceiling only so a broken spawn fails instead of wedging the suite; a
    child that exits without signalling short-circuits it immediately.
    """
    ready = tmp_path / "child-ready"
    pids: list[int] = []
    backoffs: list[float] = []
    real_popen = acli_subprocess.subprocess.Popen

    def ready_gated_popen(*args, **kwargs):
        ready.unlink(missing_ok=True)  # every attempt signals afresh
        process = real_popen(*args, **kwargs)
        pids.append(process.pid)
        deadline = time.monotonic() + _POLL_CEILING_SECONDS
        while not ready.exists() and process.poll() is None:
            if time.monotonic() >= deadline:
                break
            time.sleep(_POLL_INTERVAL_SECONDS)
        return process

    monkeypatch.setattr(acli_subprocess.subprocess, "Popen", ready_gated_popen)
    monkeypatch.setattr(acli_subprocess, "_acli_call_timeout", lambda: _REAP_CALL_TIMEOUT)
    monkeypatch.setattr(acli_subprocess, "_ACLI_GRACE_SECONDS", _REAP_GRACE_SECONDS)
    monkeypatch.setattr(acli_subprocess, "_ACLI_DRAIN_SECONDS", _REAP_DRAIN_SECONDS)
    monkeypatch.setattr(acli_subprocess, "_backoff_sleep", backoffs.append)
    return _RealChild(ready=ready, pids=pids, backoffs=backoffs)


@pytest.fixture
def completing_child_timeout(monkeypatch):
    """Bound a fake child that must COMPLETE with a hang-guard, not a pass condition."""
    monkeypatch.setattr(acli_subprocess, "_acli_call_timeout", lambda: _COMPLETING_CHILD_TIMEOUT)


# ---------------------------------------------------------------------------
# Core: grandchild reap (the load-bearing assertion)
# ---------------------------------------------------------------------------


@POSIX_ONLY
def test_grandchild_process_group_reaped(tmp_path, real_child):
    """A hung child + pipe-holding grandchild are reaped; no orphaned group remains.

    Asserts (a) AcliTimeoutError terminates the call and (b) the grandchild's
    process group is gone — polled, not asserted instantaneously (spike note: the
    grandchild can die just after a naive check). The grandchild signals readiness
    only after writing its pidfile, so the pidfile assertion below is ordered by an
    observed event rather than by the injected window out-racing interpreter start.
    """
    pidfile = tmp_path / "grandchild.pid"
    start = time.monotonic()
    with pytest.raises(acli_subprocess.AcliTimeoutError):
        acli_subprocess._run_acli(
            [str(pidfile), str(real_child.ready)],
            acli_cmd=_fake_cmd(_GRANDCHILD_HANG),
            retry_on_timeout=False,
        )
    elapsed = time.monotonic() - start
    # Injected window: call_timeout(0.2) + GRACE(0.1) + DRAIN(0.1) ~= 0.4s total. The
    # ceiling below stays at 10s and is deliberately NOT shrunk alongside it.
    # timing: hang-guard — 10s is ~25x the 0.4s injected reap window
    assert elapsed < 10, f"reap took too long: {elapsed:.1f}s"

    # The grandchild wrote its PID; its process group must be gone. Poll, because
    # the kill+reap is asynchronous w.r.t. the grandchild actually dying.
    assert pidfile.exists(), "grandchild never recorded its PID"
    gpid = int(pidfile.read_text())
    survivors = _wait_until_gone([gpid])
    if survivors:
        # Final cleanup so we never leak in CI, then fail loudly.
        for pid in survivors:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
        pytest.fail(f"grandchild PID {gpid} survived the process-group reap")


# ---------------------------------------------------------------------------
# Retry semantics on timeout
# ---------------------------------------------------------------------------


@POSIX_ONLY
def test_write_not_retried_on_timeout(tmp_path, real_child):
    """retry_on_timeout=False -> exactly ONE spawn, then AcliTimeoutError.

    The child appends its spawn marker BEFORE signalling ready, so the count below
    reads a settled file: a second spawn would be visible however slow the host is.
    """
    counter = tmp_path / "spawns"
    counter.write_text("")
    with pytest.raises(acli_subprocess.AcliTimeoutError):
        acli_subprocess._run_acli(
            [str(counter), str(real_child.ready)],
            acli_cmd=_fake_cmd(_COUNT_THEN_HANG),
            retry_on_timeout=False,
        )
    assert counter.read_text() == "x", "a timed-out WRITE must not be retried"
    assert len(real_child.pids) == 1, f"expected one spawn, got {real_child.pids}"


@POSIX_ONLY
def test_read_retried_then_terminal(real_child):
    """retry_on_timeout=True -> retries up to _MAX_ATTEMPTS then AcliTimeoutError.

    Every attempt is gated on its child signalling ready, so "three real sessions"
    is proven by three observed spawns rather than by three deadlines happening to
    outlast three interpreter starts. The retry schedule is recorded, not slept.
    """
    with pytest.raises(acli_subprocess.AcliTimeoutError):
        acli_subprocess._run_acli(
            [str(real_child.ready)],
            acli_cmd=_fake_cmd(_SIMPLE_HANG),
            retry_on_timeout=True,
        )

    assert len(real_child.pids) == acli_subprocess._MAX_ATTEMPTS, (
        "a READ should retry up to _MAX_ATTEMPTS times before going terminal"
    )
    assert len(set(real_child.pids)) == len(real_child.pids), (
        "each retry must spawn a distinct child session"
    )
    assert real_child.backoffs == [2, 4], (
        f"expected logical retry backoff [2, 4], got {real_child.backoffs}"
    )

    survivors = _wait_until_gone(real_child.pids)
    assert not survivors, f"timed-out ACLI child PIDs survived cleanup: {sorted(survivors)}"


def test_acli_timeout_error_is_not_builtin_timeout_error():
    """AcliTimeoutError must NOT subclass builtin TimeoutError (spike E4).

    Otherwise apply_outbound._call_with_retry (``except TimeoutError``) would
    blind-retry a timed-out write, re-introducing the duplicate-write bug.
    """
    assert not issubclass(acli_subprocess.AcliTimeoutError, TimeoutError)
    err = acli_subprocess.AcliTimeoutError(["acli"], 1.0)
    assert not isinstance(err, TimeoutError)


# ---------------------------------------------------------------------------
# Decode-on-kill + partial capture + no-fabricated-success
# ---------------------------------------------------------------------------


@POSIX_ONLY
def test_truncated_utf8_does_not_crash_reap_path(real_child):
    """errors='replace' -> a truncated multibyte lead must not raise on the reap path.

    With errors='strict', communicate()'s final decode raises UnicodeDecodeError
    on the cleanup path, masking the timeout. The terminal error must be
    AcliTimeoutError, not UnicodeDecodeError. The child signals ready only after
    the lead byte is in the pipe, so the decode path is always exercised.
    """
    with pytest.raises(acli_subprocess.AcliTimeoutError):
        acli_subprocess._run_acli(
            [str(real_child.ready)],
            acli_cmd=_fake_cmd(_TRUNCATED_UTF8_THEN_HANG),
            retry_on_timeout=False,
        )


@POSIX_ONLY
def test_partial_stdout_captured_on_timeout(real_child):
    """Partial stdout emitted before the hang is carried on AcliTimeoutError.

    The child flushes stdout BEFORE signalling ready, so the bytes are already in
    the pipe when the injected window starts — the capture assertion no longer
    depends on the deadline outlasting interpreter startup.
    """
    with pytest.raises(acli_subprocess.AcliTimeoutError) as ei:
        acli_subprocess._run_acli(
            [str(real_child.ready)],
            acli_cmd=_fake_cmd(_PARTIAL_THEN_HANG),
            retry_on_timeout=False,
        )
    assert ei.value.partial_stdout is not None
    assert "partial-output-here" in ei.value.partial_stdout


@POSIX_ONLY
def test_check_mutation_failure_not_called_on_killed_child(monkeypatch, real_child):
    """A killed child must never reach _check_mutation_failure (no fabricated success)."""
    called = {"n": 0}
    real = acli_subprocess._check_mutation_failure

    def _spy(stdout, cmd):
        called["n"] += 1
        return real(stdout, cmd)

    monkeypatch.setattr(acli_subprocess, "_check_mutation_failure", _spy)
    with pytest.raises(acli_subprocess.AcliTimeoutError):
        acli_subprocess._run_acli(
            [str(real_child.ready)],
            acli_cmd=_fake_cmd(_SIMPLE_HANG),
            retry_on_timeout=False,
        )
    assert called["n"] == 0, "_check_mutation_failure ran on a killed/timed-out child"


# ---------------------------------------------------------------------------
# Classification guard: each read passes True, each write defaults False
# ---------------------------------------------------------------------------


def _make_client():
    return acli_mod.AcliClient(
        "https://example.atlassian.net",
        "user@example.com",
        "token",
        jira_project="TEST",
        acli_cmd=_fake_cmd(_FAST_OK),
    )


@pytest.fixture
def record_run(monkeypatch):
    """Record (cmd, retry_on_timeout) for every _run_acli call; return [].

    Patches the seam module-qualified name so both AcliClient._run and the
    acli_cli_ops free functions are covered. The fake returns a fast empty-JSON
    CompletedProcess so methods complete without spawning.
    """
    calls: list[tuple[list[str], bool]] = []

    def _fake_run_acli(cmd, *, acli_cmd=None, retry_on_timeout=False, call_timeout=None):
        calls.append((cmd, retry_on_timeout))
        return subprocess.CompletedProcess(cmd, 0, "[]", "")

    monkeypatch.setattr(acli_subprocess, "_run_acli", _fake_run_acli)
    return calls


def test_reads_pass_retry_on_timeout_true(record_run):
    """The 5 READ call sites must explicitly pass retry_on_timeout=True.

    A new caller that mis-defaults a read to False is caught here.
    """
    client = _make_client()

    # READ sites — route A (self._run) and route B (free functions). Some
    # methods post-process the (empty) fake result and raise (get_issue rejects
    # an empty list) — that is fine; we only assert the recorded retry flag.
    client.search_issues("project = TEST")  # acli.py:387
    client.get_comments("TEST-1")  # acli.py:590
    client.get_issue_link_types()  # acli_graph.py:77
    with pytest.raises(RuntimeError):
        acli_cli_ops.get_issue("TEST-1", acli_cmd=_fake_cmd(_FAST_OK))  # acli_cli_ops.py:454
    acli_cli_ops.get_comments("TEST-1", acli_cmd=_fake_cmd(_FAST_OK))  # acli_cli_ops.py:524

    assert record_run, "no _run_acli calls were recorded"
    assert all(retry is True for _cmd, retry in record_run), (
        f"a READ did not pass retry_on_timeout=True: {[(c, r) for c, r in record_run]}"
    )
    assert len(record_run) == 5


def test_writes_default_retry_on_timeout_false(record_run):
    """WRITE call sites must resolve to retry_on_timeout=False (safe-by-omission)."""
    client = _make_client()

    client.set_relationship("TEST-1", "TEST-2", "Blocks")  # acli_graph.py:473
    client.delete_issue_link("10000")  # acli_graph.py:513
    client.update_comment("TEST-1", "1", "body")  # acli_graph.py:433
    client.add_label("TEST-1", "lbl")  # acli_graph.py:142
    client.remove_label("TEST-1", "lbl")  # acli_graph.py:185
    # add_comment post-processes the (empty-list) fake result and raises
    # (its parser rejects a non-dict payload) — fine; we assert the flag only.
    with pytest.raises(RuntimeError):
        acli_cli_ops.add_comment(
            "TEST-1", "body", acli_cmd=_fake_cmd(_FAST_OK)
        )  # acli_cli_ops.py:484

    assert record_run, "no _run_acli calls were recorded"
    assert all(retry is False for _cmd, retry in record_run), (
        f"a WRITE did not default retry_on_timeout=False: {[(c, r) for c, r in record_run]}"
    )


def test_delete_routes_through_chokepoint(record_run):
    """delete_issue routes through _run_acli (WRITE, retry_on_timeout=False)."""
    client = _make_client()
    client.delete_issue("TEST-1")
    assert record_run, "delete_issue did not route through _run_acli"
    cmd, retry = record_run[-1]
    assert "delete" in cmd
    assert retry is False


# ---------------------------------------------------------------------------
# C4 (943f): 429 rate-limit backoff in the live _run_acli retry loop
# ---------------------------------------------------------------------------
import logging  # noqa: E402

_FAKE_429_THEN_OK = r"""
import sys, os
counter = os.environ["FAKE_429_COUNTER"]
n = int(open(counter).read()) if os.path.exists(counter) else 0
open(counter, "w").write(str(n + 1))
if n == 0:
    sys.stderr.write("ACLI error: HTTP 429 Too Many Requests\nRetry-After: 1\n")
    sys.exit(1)
sys.stdout.write("[]")
"""


def test_rate_limit_backoff_honors_retry_after() -> None:
    assert acli_subprocess._rate_limit_backoff(0, "HTTP 429\nRetry-After: 7") == 7.0
    # A hostile/huge Retry-After is capped at the ceiling.
    assert acli_subprocess._rate_limit_backoff(0, "429 Retry-After: 99999") == 60.0


def test_rate_limit_backoff_jitters_without_retry_after() -> None:
    d = acli_subprocess._rate_limit_backoff(0, "429 Too Many Requests")
    assert d is not None and 2.0 <= d <= 3.0  # 2**1 + jitter[0,1]


def test_rate_limit_backoff_none_for_non_429() -> None:
    assert acli_subprocess._rate_limit_backoff(0, "some other error") is None
    assert acli_subprocess._rate_limit_backoff(0, None) is None


def test_run_acli_429_retries_with_rate_limit_backoff(
    tmp_path, monkeypatch, caplog, completing_child_timeout
) -> None:
    """A 429 exit routes through the rate-limit backoff (honoring Retry-After), the call
    succeeds on retry, and NO uniform 2s sleep is used (add-on, not double-sleep).

    Both fake children must COMPLETE here, so the call timeout is a generous
    hang-guard rather than the module-wide 1s this test used to inherit — that
    ceiling was a latent flake whenever two interpreter starts exceeded a second."""
    monkeypatch.setenv("FAKE_429_COUNTER", str(tmp_path / "n"))
    delays: list[float] = []
    # Patch the narrow retry-backoff SEAM, not the module-global time.sleep. The
    # latter would also capture CPython's subprocess.Popen._wait busy-wait poll
    # sleeps (an exponential 0.0005→0.05s series emitted from communicate() whose
    # iteration count depends on OS reap latency), making the assertion flaky under
    # load. _backoff_sleep isolates the retry-backoff schedule deterministically.
    monkeypatch.setattr(acli_subprocess, "_backoff_sleep", lambda s: delays.append(s))
    with caplog.at_level(logging.WARNING):
        result = acli_subprocess._run_acli(["search", "x"], acli_cmd=_fake_cmd(_FAKE_429_THEN_OK))
    assert result.returncode == 0 and result.stdout == "[]"
    # Exactly one retry backoff, honoring Retry-After=1 (no uniform 2s/4s backoff).
    assert delays == [1.0], f"expected one Retry-After=1 backoff, got {delays}"
    assert any("429" in r.message for r in caplog.records)


def test_set_relationship_emits_correct_acli_flag_order(record_run):
    """set_relationship(from, to) must emit ``--out to --in from`` (bug 3b86).

    ACLI's ``--out``/``--in`` are inverted vs the naive reading — the ``--in`` issue is the
    BLOCKER — so ``--out to_key --in from_key`` is what makes the link read "from blocks to".
    Passing them the other way (the old code) reversed every written link. Pinning the ACTUAL
    emitted command is the only unit-level guard for link direction: a stub client that mocks
    ``set_relationship`` cannot catch a flag-order regression.
    """
    client = _make_client()
    client.set_relationship("FROM-1", "TO-2", "Blocks")
    link_cmds = [c for c, _retry in record_run if "link" in c and "create" in c]
    assert link_cmds, f"no `link create` command recorded: {[c for c, _ in record_run]!r}"
    cmd = link_cmds[0]
    assert cmd[cmd.index("--out") + 1] == "TO-2", f"--out must carry to_key (TO-2): {cmd!r}"
    assert cmd[cmd.index("--in") + 1] == "FROM-1", f"--in must carry from_key (FROM-1): {cmd!r}"


# ---------------------------------------------------------------------------
# Ticket 2048-d289: explicit per-call timeout (operation-scoped capture)
# ---------------------------------------------------------------------------


@POSIX_ONLY
def test_explicit_call_timeout_bounds_call_without_ambient_resolve(real_child, monkeypatch):
    """An explicit call_timeout bounds the subprocess wait, and the ambient
    _acli_call_timeout resolve is never consulted."""

    def _boom() -> float:
        raise AssertionError("ambient _acli_call_timeout must not be consulted")

    monkeypatch.setattr(acli_subprocess, "_acli_call_timeout", _boom)
    with pytest.raises(acli_subprocess.AcliTimeoutError):
        acli_subprocess._run_acli(
            [str(real_child.ready)],
            acli_cmd=_fake_cmd(_SIMPLE_HANG),
            retry_on_timeout=False,
            call_timeout=_REAP_CALL_TIMEOUT,
        )


def test_non_positive_call_timeout_falls_back_to_ambient(monkeypatch):
    """None/0/negative call_timeout keeps the ambient resolve — the legacy floor
    for direct constructions and entry points outside a composed runtime."""
    monkeypatch.setattr(acli_subprocess, "_acli_call_timeout", lambda: 77.0)
    assert acli_subprocess._effective_call_timeout(None) == 77.0
    assert acli_subprocess._effective_call_timeout(0) == 77.0
    assert acli_subprocess._effective_call_timeout(-3) == 77.0
    assert acli_subprocess._effective_call_timeout(45) == 45


# ---------------------------------------------------------------------------
# REB-3115 S1 T2 (AC6) — timeout cleanup passes on non-POSIX too
# ---------------------------------------------------------------------------
#
# The POSIX tests above prove the process-GROUP reap (killpg). AC6 additionally
# requires the non-POSIX fallback — a plain ``proc.kill()`` + bounded wait — to
# reap a timed-out child. The shared reaper (``rebar._proc.reap_process_group``,
# reached through ``acli_subprocess._reap_process_group``) branches on ``os.name``;
# patching it to a non-"posix" value on this POSIX host drives the fallback branch
# against a REAL hanging child. ``start_new_session`` is a POSIX-only spawn kwarg
# but harmless here; only the reap path differs.


@POSIX_ONLY  # we still need a real fork()able child; only the reaper branch is forced non-POSIX
def test_non_posix_timeout_reaps_the_direct_child(real_child, monkeypatch):
    """With ``os.name`` forced non-POSIX in the shared reaper, a hung child is still
    reaped via the ``proc.kill()`` fallback and the call ends in AcliTimeoutError."""
    from rebar import _proc

    monkeypatch.setattr(_proc.os, "name", "nt")

    with pytest.raises(acli_subprocess.AcliTimeoutError):
        acli_subprocess._run_acli(
            [str(real_child.ready)],
            acli_cmd=_fake_cmd(_SIMPLE_HANG),
            retry_on_timeout=False,
        )

    assert len(real_child.pids) == 1, f"expected one spawn, got {real_child.pids}"
    survivors = _wait_until_gone(real_child.pids)
    if survivors:
        for pid in survivors:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
        pytest.fail(f"non-POSIX fallback did not reap child PID(s): {sorted(survivors)}")


def test_non_posix_reaper_uses_kill_not_killpg(monkeypatch):
    """Unit-level proof of the branch: on non-POSIX the shared reaper calls
    ``proc.kill()`` and waits, and NEVER reaches ``os.killpg`` (absent on Windows)."""
    import logging

    from rebar import _proc

    monkeypatch.setattr(_proc.os, "name", "nt")

    class _FakeProc:
        pid = 4321

        def __init__(self) -> None:
            self.killed = False

        def kill(self) -> None:
            self.killed = True

        def wait(self, timeout=None) -> int:
            return 0

    def _boom_killpg(*_a, **_k):  # pragma: no cover - must never be called
        raise AssertionError("killpg must not be used on the non-POSIX path")

    monkeypatch.setattr(_proc.os, "killpg", _boom_killpg, raising=False)
    proc = _FakeProc()
    _proc.reap_process_group(
        proc, grace=0.01, drain=0.01, label="acli", logger=logging.getLogger("t")
    )
    assert proc.killed, "non-POSIX reap must call proc.kill()"
