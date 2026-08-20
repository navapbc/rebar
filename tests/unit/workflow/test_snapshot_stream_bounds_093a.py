"""Bug 093a: the ``git archive`` stream in the workflow snapshot must be bounded.

``snapshot_at_ref`` streamed ``git archive`` through a pipe with three defects in one
call, all of which park the caller on an unbounded wait:

1. **No timeout.** Neither the ``Popen`` nor the trailing ``communicate()`` carried one,
   and the blocking read is inside ``tarfile.__read`` where a ``communicate(timeout=…)``
   could not reach it anyway. A promisor lazy-fetch stall (a partial clone whose promisor
   remote accepts the connection and then never replies) held the process at ``STAT=SN,
   %CPU 0.0`` for 4m26s with zero bytes moved, and was still blocked when killed.
2. **``stderr=PIPE`` never drained** while stdout is consumed. A child that writes past the
   ~64 KiB pipe buffer to stderr blocks writing stderr, therefore stops writing stdout, and
   the parent blocks forever in ``tarfile.__read``. A classic undrained-pipe deadlock that
   needs no network and no partial clone. Note this is a *proven latent* deadlock, not an
   observed field failure: a real ``git archive`` emits 0 bytes of stderr because git
   suppresses progress when stderr is not a TTY, and ``Popen`` gives it a pipe.
3. **stdin inherited** — unlike ``_store/push.py`` and ``llm/enrich_drain.py``, which both
   pass ``stdin=DEVNULL``, so a credential or host-key prompt could block here too.

The first two tests drive the REAL ``snapshot_at_ref`` against a fake ``git`` on ``PATH``
and run it in a CHILD PROCESS under a hard wall-clock bound, so a regression FAILS instead
of hanging CI forever (the pre-fix code never returns from either scenario).

Two constraints carried in from closed tickets:

* ``subprocess.TimeoutExpired`` is neither an ``OSError`` nor a ``CalledProcessError``, so
  it escapes ordinary ``except`` tuples, and ``TimeoutExpired.cmd`` carries any
  ``user:token@host`` URL — found the hard way in ``dominant-northbound-blackrhino``
  (``77e1-7f82-98e6-4fed``). Turning a hang into a credential disclosure is not a fix, so
  the redaction is pinned here.
* Elapsed time is the wrong axis for a stalled transfer (``_snapshot/git_fetch.py``);
  ``suave-constant-cow`` (``12e4-8c74-a738-4014``) built the reusable ``stall_abort_args()``
  seam. ``git archive`` reaches the network through the promisor lazy-fetch path, so that
  seam is spliced here rather than only shortening a wall clock.

Everything here is offline: no network, no LLM.
"""

from __future__ import annotations

import io
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock

import pytest

from rebar.llm.workflow import snapshot as snap

# Hard outer bound on the child that drives ``snapshot_at_ref``. Generous enough for a
# cold import of rebar, far below any "hung CI job" horizon.
_HARD_BOUND_SECONDS = 90
# The in-child archive ceiling for the stall test — the MECHANISM is unchanged, only the
# window shrinks so the test finishes. Never lower the module's shipped 300s default.
_TIGHT_ARCHIVE_TIMEOUT = "3"
# Comfortably past the ~64 KiB pipe buffer that makes the undrained-stderr write block.
_STDERR_FLOOD_BYTES = 512 * 1024

_CRED_URL = "https://bot:s3cr3t-bot-token@git.example.invalid/repo.git"
_TOKEN = "s3cr3t-bot-token"


# ── fixtures: a real repo, and a fake ``git`` that misbehaves only on ``archive`` ──


def _git(*args, cwd) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    _git("config", "user.email", "t@e.com", cwd=repo)
    _git("config", "user.name", "T", cwd=repo)
    (repo / "a.py").write_text("print(1)\n")
    (repo / "pkg").mkdir()
    (repo / "pkg" / "b.py").write_text("x = 2\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", "init", cwd=repo)
    return repo


_FAKE_GIT = """\
#!/usr/bin/env python3
# A ``git`` shim that misbehaves ONLY on ``archive`` and delegates everything else.
import os
import subprocess
import sys
import time

real = os.environ["REBAR_TEST_REAL_GIT"]
args = sys.argv[1:]
if "archive" not in args:
    os.execv(real, [real, *args])

pidfile = os.environ.get("REBAR_TEST_PIDFILE")
if pidfile:
    with open(pidfile, "w") as fh:
        fh.write(str(os.getpid()))

# Build a genuine tar with the real git, then replay it badly.
done = subprocess.run([real, *args], capture_output=True)
tar = done.stdout
mode = os.environ["REBAR_TEST_ARCHIVE_MODE"]
if mode == "stderr_flood":
    half = max(1, len(tar) // 2)
    sys.stdout.buffer.write(tar[:half])
    sys.stdout.buffer.flush()
    # Blocks once the pipe buffer fills unless the parent drains stderr concurrently.
    sys.stderr.buffer.write(b"x" * int(os.environ["REBAR_TEST_FLOOD_BYTES"]))
    sys.stderr.buffer.flush()
    sys.stdout.buffer.write(tar[half:])
    sys.stdout.buffer.flush()
elif mode == "stall":
    # A partial tar, then dead air forever — the promisor lazy-fetch stall in miniature.
    sys.stdout.buffer.write(tar[:512])
    sys.stdout.buffer.flush()
    time.sleep(3600)
sys.exit(0)
"""

_DRIVER = """\
import os
import sys

from rebar.llm.workflow import snapshot as snap

snap._GIT_TIMEOUT = float(os.environ["REBAR_TEST_ARCHIVE_TIMEOUT"])
try:
    dest = snap.snapshot_at_ref("HEAD", sys.argv[1])
except snap.SnapshotError as exc:
    print("SNAPSHOT_ERROR", exc)
except BaseException as exc:  # noqa: BLE001 - the driver reports whatever escaped
    print("OTHER_ERROR", type(exc).__name__, exc)
else:
    print("OK", dest)
"""


def _fake_git_dir(tmp_path: Path) -> Path:
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    shim = bindir / "git"
    shim.write_text(_FAKE_GIT)
    shim.chmod(0o755)
    return bindir


def _real_git() -> str:
    from shutil import which

    found = which("git")
    assert found, "a real git is required to build the fixture repo"
    return found


def _drive_snapshot(
    repo: Path,
    bindir: Path,
    *,
    mode: str,
    archive_timeout: str = "300",
    pidfile: Path | None = None,
) -> tuple[subprocess.CompletedProcess[str], float]:
    """Run the REAL ``snapshot_at_ref`` in a child, under a hard wall-clock bound."""
    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["REBAR_TEST_REAL_GIT"] = _real_git()
    env["REBAR_TEST_ARCHIVE_MODE"] = mode
    env["REBAR_TEST_ARCHIVE_TIMEOUT"] = archive_timeout
    env["REBAR_TEST_FLOOD_BYTES"] = str(_STDERR_FLOOD_BYTES)
    if pidfile is not None:
        env["REBAR_TEST_PIDFILE"] = str(pidfile)
    started = time.monotonic()
    proc = subprocess.Popen(
        [sys.executable, "-c", _DRIVER, str(repo)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        text=True,
        env=env,
        start_new_session=True,  # own process group, so a timeout can reap the whole tree
    )
    try:
        out, err = proc.communicate(timeout=_HARD_BOUND_SECONDS)
    except subprocess.TimeoutExpired:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        out, err = proc.communicate()
        pytest.fail(
            f"snapshot_at_ref did not return within {_HARD_BOUND_SECONDS}s "
            f"(mode={mode}) — the unbounded stream is back. stdout={out!r} stderr={err[-2000:]!r}"
        )
    elapsed = time.monotonic() - started
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err), elapsed


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - alive but not ours
        return True
    return True


# ── AC1: an stderr flood must not deadlock the parent ────────────────────────


def test_stderr_flood_does_not_deadlock_the_archive_stream(tmp_path: Path) -> None:
    """A child writing past the pipe buffer to stderr must not wedge the extraction.

    Pre-fix this never returns: the child blocks on its stderr write, therefore stops
    writing stdout, and the parent blocks in ``tarfile.__read``. Post-fix stderr is drained
    concurrently, so the tar streams to completion and the snapshot is built normally.
    """
    repo = _repo(tmp_path)
    bindir = _fake_git_dir(tmp_path)
    done, _ = _drive_snapshot(repo, bindir, mode="stderr_flood")
    assert done.stdout.startswith("OK"), (done.stdout, done.stderr[-2000:])
    dest = Path(done.stdout.split(" ", 1)[1].strip())
    assert (dest / "a.py").read_text() == "print(1)\n"
    assert (dest / "pkg" / "b.py").read_text() == "x = 2\n"


# ── AC2: a stalled child is bounded AND terminated, not orphaned ─────────────


def test_stalled_archive_is_bounded_and_the_child_is_reaped(tmp_path: Path) -> None:
    """Dead air on stdout must end in a bounded ``SnapshotError``, with no orphan left."""
    repo = _repo(tmp_path)
    bindir = _fake_git_dir(tmp_path)
    pidfile = tmp_path / "child.pid"
    done, elapsed = _drive_snapshot(
        repo,
        bindir,
        mode="stall",
        archive_timeout=_TIGHT_ARCHIVE_TIMEOUT,
        pidfile=pidfile,
    )
    assert done.stdout.startswith("SNAPSHOT_ERROR"), (done.stdout, done.stderr[-2000:])
    assert "timed out" in done.stdout, done.stdout
    # The bound is the mechanism: well inside the hard outer bound, not merely "eventually".
    assert elapsed < _HARD_BOUND_SECONDS / 2, elapsed
    # And the stalled child is terminated rather than left running.
    assert pidfile.exists(), "the fake git never recorded its pid"
    stalled_pid = int(pidfile.read_text())
    deadline = time.monotonic() + 10
    while _alive(stalled_pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not _alive(stalled_pid), f"orphan git archive child {stalled_pid} survived"


# ── AC3: stdin is not inherited, and the stall abort is armed ────────────────


def test_archive_child_gets_devnull_stdin_and_the_stall_abort(tmp_path: Path) -> None:
    """``stdin=DEVNULL`` (no prompt can block) and the 12e4 throughput abort is spliced.

    ``git archive`` reaches the network through the promisor lazy-fetch path, so elapsed
    time alone is the wrong instrument; the ``-c`` pairs must precede the subcommand
    because git only accepts ``-c`` as a top-level option.
    """
    repo = _repo(tmp_path)
    seen: dict[str, object] = {}
    real_popen = subprocess.Popen

    def _spy(argv, **kwargs):
        if "archive" in argv:
            seen["argv"] = list(argv)
            seen["kwargs"] = dict(kwargs)
        return real_popen(argv, **kwargs)

    with mock.patch.object(snap.subprocess, "Popen", _spy):
        snap.snapshot_at_ref("HEAD", str(repo))

    assert seen, "git archive was never spawned"
    kwargs = seen["kwargs"]
    assert kwargs["stdin"] is subprocess.DEVNULL, kwargs
    argv = seen["argv"]
    assert "http.lowSpeedLimit=1000" in argv, argv
    assert "http.lowSpeedTime=10" in argv, argv
    assert argv.index("http.lowSpeedTime=10") < argv.index("archive"), argv


# ── AC4: a TimeoutExpired here cannot leak a credential-bearing command line ──


class _CredTimeoutProc:
    """A ``Popen`` stand-in whose ``wait`` raises the 77e1 credential-bearing timeout."""

    def __init__(self, tar_bytes: bytes) -> None:
        self.stdout = io.BytesIO(tar_bytes)
        self.stderr = io.BytesIO(b"")
        self.returncode: int | None = None
        self.pid = -1

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        raise subprocess.TimeoutExpired(cmd=["git", "fetch", _CRED_URL], timeout=timeout or 1)

    def communicate(self, timeout: float | None = None):
        raise subprocess.TimeoutExpired(cmd=["git", "fetch", _CRED_URL], timeout=timeout or 1)

    def kill(self) -> None:
        self.returncode = -9


def test_timeout_expired_cannot_leak_a_credential_url(tmp_path: Path) -> None:
    """A ``TimeoutExpired`` must surface as a redacted ``SnapshotError``, not raw.

    ``TimeoutExpired`` is neither ``OSError`` nor ``CalledProcessError``, so without an
    explicit conversion it escapes this module's error vocabulary entirely — and its
    ``cmd`` carries the ``user:token@host`` URL verbatim (77e1).
    """
    repo = _repo(tmp_path)
    sha = snap.resolve_sha("HEAD", str(repo))
    tar = subprocess.run(
        ["git", "-C", str(repo), "archive", "--format=tar", sha],
        capture_output=True,
        check=True,
    ).stdout

    real_popen = subprocess.Popen

    def _spy(argv, **kwargs):
        if "archive" not in argv:  # resolve_sha's rev-parse must still run for real
            return real_popen(argv, **kwargs)
        return _CredTimeoutProc(tar)

    with mock.patch.object(snap.subprocess, "Popen", _spy):
        with pytest.raises(snap.SnapshotError) as excinfo:
            snap.snapshot_at_ref("HEAD", str(repo))

    message = str(excinfo.value)
    assert _TOKEN not in message, message
    assert "bot:" not in message, message
    assert "***" in message, message


def test_redactor_masks_url_userinfo_in_any_shape() -> None:
    """The redactor masks the whole userinfo, for both list and string ``cmd`` forms."""
    as_list = snap._redact_cmd(["git", "fetch", _CRED_URL])
    as_string = snap._redact_cmd(f"git fetch {_CRED_URL}")
    for rendered in (as_list, as_string):
        assert _TOKEN not in rendered, rendered
        assert "bot:" not in rendered, rendered
        assert "git.example.invalid" in rendered, rendered


# ── AC5-adjacent: the shipped bound is not weakened ──────────────────────────


def test_module_declares_a_positive_git_timeout() -> None:
    """Same shape as 747f / 77e1: the bound is a module constant, and it is not shrunk."""
    timeout = getattr(snap, "_GIT_TIMEOUT", None)
    assert timeout is not None, "snapshot.py declares no _GIT_TIMEOUT"
    assert isinstance(timeout, (int, float))
    assert timeout >= 300, "77e1/747f chose 300s deliberately; do not lower it"
