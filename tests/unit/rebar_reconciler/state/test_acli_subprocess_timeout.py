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

import os
import signal
import subprocess
import sys
import time

import pytest

# The reconciler engine is on sys.path via the package conftest; import flat.
from rebar_reconciler import acli as acli_mod
from rebar_reconciler import acli_cli_ops, acli_subprocess

POSIX_ONLY = pytest.mark.skipif(os.name != "posix", reason="process-group reaping is POSIX-only")


# ---------------------------------------------------------------------------
# Fake-binary programs (run as the `acli` executable)
# ---------------------------------------------------------------------------

# Forks a grandchild that inherits the stdout PIPE and hangs forever, writes the
# grandchild PID to a pidfile, then the child itself hangs holding the pipe. This
# is the exact gotcha-1 shape: subprocess.run(timeout=) would orphan the
# grandchild; only a process-GROUP kill reaps it.
_GRANDCHILD_HANG = r"""
import os, sys, time
pidfile = sys.argv[1]
pid = os.fork()
if pid == 0:
    # grandchild: keep the inherited stdout pipe open and hang
    with open(pidfile, "w") as f:
        f.write(str(os.getpid()))
    time.sleep(3600)
    os._exit(0)
# parent (the direct child): also hang, holding the pipe
time.sleep(3600)
"""

# A simple child that just hangs (no grandchild) — used for retry/spawn-count tests.
_SIMPLE_HANG = r"""
import time
time.sleep(3600)
"""

# Appends a marker per invocation so we can count spawns, then hangs.
_COUNT_THEN_HANG = r"""
import sys, time
with open(sys.argv[1], "a") as f:
    f.write("x")
time.sleep(3600)
"""

# Emits a truncated multibyte UTF-8 lead byte on stdout then hangs. With
# errors='strict' the cleanup-path decode would raise UnicodeDecodeError; with
# errors='replace' it must not.
_TRUNCATED_UTF8_THEN_HANG = r"""
import sys, time
sys.stdout.buffer.write(b"ok-\xe2\x82")
sys.stdout.buffer.flush()
time.sleep(3600)
"""

# Emits partial stdout then hangs — to assert partial capture on timeout.
_PARTIAL_THEN_HANG = r"""
import sys, time
sys.stdout.write("partial-output-here")
sys.stdout.flush()
time.sleep(3600)
"""

# A fast no-op that prints valid JSON and exits 0 — used by the classification
# guard so client methods complete without timing out.
_FAST_OK = r"""
import sys
sys.stdout.write("[]")
"""


def _fake_cmd(program: str, *args: str) -> list[str]:
    """Build an acli_cmd that runs *program* as the fake binary."""
    return [sys.executable, "-c", program, *args]


@pytest.fixture(autouse=True)
def _short_timeout(monkeypatch):
    """Use a small per-call timeout so tests stay fast."""
    monkeypatch.setenv("REBAR_ACLI_TIMEOUT", "1")
    # Shrink grace/drain too so the reap window is tight in tests.
    monkeypatch.setattr(acli_subprocess, "_ACLI_GRACE_SECONDS", 1)
    monkeypatch.setattr(acli_subprocess, "_ACLI_DRAIN_SECONDS", 1)


# ---------------------------------------------------------------------------
# Core: grandchild reap (the load-bearing assertion)
# ---------------------------------------------------------------------------


@POSIX_ONLY
def test_grandchild_process_group_reaped(tmp_path):
    """A hung child + pipe-holding grandchild are reaped; no orphaned group remains.

    Asserts (a) AcliTimeoutError within ~call_timeout+GRACE+DRAIN and (b) the
    grandchild's process group is gone — polled, not asserted instantaneously
    (spike note: the grandchild can die just after a naive check).
    """
    pidfile = tmp_path / "grandchild.pid"
    start = time.monotonic()
    with pytest.raises(acli_subprocess.AcliTimeoutError):
        acli_subprocess._run_acli(
            [str(pidfile)],
            acli_cmd=_fake_cmd(_GRANDCHILD_HANG),
            retry_on_timeout=False,
        )
    elapsed = time.monotonic() - start
    # call_timeout(1) + GRACE(1) + DRAIN(1) with headroom.
    assert elapsed < 10, f"reap took too long: {elapsed:.1f}s"

    # The grandchild wrote its PID; its process group must be gone. Poll, because
    # the kill+reap is asynchronous w.r.t. the grandchild actually dying.
    assert pidfile.exists(), "grandchild never recorded its PID"
    gpid = int(pidfile.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(gpid, 0)  # 0 == existence probe
        except ProcessLookupError:
            break  # gone — reaped
        time.sleep(0.05)
    else:
        # Final cleanup so we never leak in CI, then fail loudly.
        try:
            os.kill(gpid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        pytest.fail(f"grandchild PID {gpid} survived the process-group reap")


# ---------------------------------------------------------------------------
# Retry semantics on timeout
# ---------------------------------------------------------------------------


@POSIX_ONLY
def test_write_not_retried_on_timeout(tmp_path):
    """retry_on_timeout=False -> exactly ONE spawn, then AcliTimeoutError."""
    counter = tmp_path / "spawns"
    counter.write_text("")
    with pytest.raises(acli_subprocess.AcliTimeoutError):
        acli_subprocess._run_acli(
            [str(counter)],
            acli_cmd=_fake_cmd(_COUNT_THEN_HANG),
            retry_on_timeout=False,
        )
    assert counter.read_text() == "x", "a timed-out WRITE must not be retried"


@POSIX_ONLY
def test_read_retried_then_terminal(tmp_path):
    """retry_on_timeout=True -> retries up to _MAX_ATTEMPTS then AcliTimeoutError."""
    counter = tmp_path / "spawns"
    counter.write_text("")
    with pytest.raises(acli_subprocess.AcliTimeoutError):
        acli_subprocess._run_acli(
            [str(counter)],
            acli_cmd=_fake_cmd(_COUNT_THEN_HANG),
            retry_on_timeout=True,
        )
    assert len(counter.read_text()) == acli_subprocess._MAX_ATTEMPTS, (
        "a READ should retry up to _MAX_ATTEMPTS times before going terminal"
    )


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
def test_truncated_utf8_does_not_crash_reap_path():
    """errors='replace' -> a truncated multibyte lead must not raise on the reap path.

    With errors='strict', communicate()'s final decode raises UnicodeDecodeError
    on the cleanup path, masking the timeout. The terminal error must be
    AcliTimeoutError, not UnicodeDecodeError.
    """
    with pytest.raises(acli_subprocess.AcliTimeoutError):
        acli_subprocess._run_acli(
            [],
            acli_cmd=_fake_cmd(_TRUNCATED_UTF8_THEN_HANG),
            retry_on_timeout=False,
        )


@POSIX_ONLY
def test_partial_stdout_captured_on_timeout():
    """Partial stdout emitted before the hang is carried on AcliTimeoutError."""
    with pytest.raises(acli_subprocess.AcliTimeoutError) as ei:
        acli_subprocess._run_acli(
            [],
            acli_cmd=_fake_cmd(_PARTIAL_THEN_HANG),
            retry_on_timeout=False,
        )
    assert ei.value.partial_stdout is not None
    assert "partial-output-here" in ei.value.partial_stdout


@POSIX_ONLY
def test_check_mutation_failure_not_called_on_killed_child(monkeypatch):
    """A killed child must never reach _check_mutation_failure (no fabricated success)."""
    called = {"n": 0}
    real = acli_subprocess._check_mutation_failure

    def _spy(stdout, cmd):
        called["n"] += 1
        return real(stdout, cmd)

    monkeypatch.setattr(acli_subprocess, "_check_mutation_failure", _spy)
    with pytest.raises(acli_subprocess.AcliTimeoutError):
        acli_subprocess._run_acli(
            [],
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

    def _fake_run_acli(cmd, *, acli_cmd=None, retry_on_timeout=False):
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
    acli_cli_ops.add_comment("TEST-1", "body", acli_cmd=_fake_cmd(_FAST_OK))  # acli_cli_ops.py:484

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


def test_run_acli_429_retries_with_rate_limit_backoff(tmp_path, monkeypatch, caplog) -> None:
    """A 429 exit routes through the rate-limit backoff (honoring Retry-After), the call
    succeeds on retry, and NO uniform 2s sleep is used (add-on, not double-sleep)."""
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
