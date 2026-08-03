"""A stalled snapshot fetch must abort on throughput, not burn the wall-clock ceiling.

Ticket ``12e4``. ``git fetch`` of the GitHub mirror intermittently wedges with the socket
open and no bytes moving; bounded only by ``_GIT_TIMEOUT`` it consumed 300s per review.
The fix hands git a *throughput-keyed* abort (``http.lowSpeedLimit`` / ``http.lowSpeedTime``).

The tests below deliberately assert on the **reason** a fetch failed, never on how long it
took: a wall-clock assertion would flake in CI and — worse — would still pass if someone
"fixed" this by lowering the timeout, which is exactly the wrong fix. The discriminator is
:func:`test_slow_but_progressing_remote_is_not_aborted`: it drives a remote that dribbles
bytes continuously, and it fails for any implementation keyed on elapsed time.
"""

from __future__ import annotations

import socket
import subprocess
import threading
import time

import pytest

from rebar._snapshot import git_fetch


def _serve(mode: str, *, seconds: float, rate: int = 0) -> tuple[int, threading.Event]:
    """Bind a local HTTP-ish remote and return its port plus a shutdown flag.

    ``mode="stall"`` sends response headers and then **zero** body bytes for ``seconds``.
    ``mode="dribble"`` sends body bytes continuously at ``rate`` bytes/second — slow, but
    always progressing."""
    stop = threading.Event()
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    srv.settimeout(0.5)
    port = srv.getsockname()[1]

    def handle(conn: socket.socket) -> None:
        try:
            conn.recv(65536)
            conn.sendall(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/x-git-upload-pack-advertisement\r\n"
                b"Content-Length: 100000000\r\n\r\n"
            )
            deadline = time.monotonic() + seconds
            chunk = b"x" * max(1, rate // 10)
            while not stop.is_set() and time.monotonic() < deadline:
                if mode == "dribble":
                    conn.sendall(chunk)
                time.sleep(0.1)
        except OSError:
            pass
        finally:
            conn.close()

    def accept_loop() -> None:
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except (TimeoutError, OSError):
                continue
            threading.Thread(target=handle, args=(conn,), daemon=True).start()
        srv.close()

    threading.Thread(target=accept_loop, daemon=True).start()
    return port, stop


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return str(root)


@pytest.fixture
def tight_stall(monkeypatch):
    """Shrink the abort window so the test is quick; the *mechanism* is unchanged."""
    monkeypatch.setenv("REBAR_SNAPSHOT_STALL_FLOOR_BYTES_PER_SEC", "1000")
    monkeypatch.setenv("REBAR_SNAPSHOT_STALL_WINDOW_SECONDS", "2")
    monkeypatch.setenv("REBAR_SNAPSHOT_STALL_ATTEMPTS", "1")
    # The wall-clock ceiling stays FAR above the abort window, so a test that passes can
    # only have passed because the throughput check fired — not because time ran out.
    monkeypatch.setattr(git_fetch, "_GIT_TIMEOUT", 60)


def test_stalled_remote_aborts_on_throughput_not_the_wall_clock(repo, tmp_path, tight_stall):
    """A remote that accepts the socket and sends nothing fails as a *stall*, fast."""
    port, stop = _serve("stall", seconds=30)
    try:
        with pytest.raises(git_fetch.SnapshotFetchError) as excinfo:
            git_fetch.fetch_origin(
                repo,
                lock_path=tmp_path / "locks" / "fetch.lock",
                remote=f"http://127.0.0.1:{port}/x.git",
            )
    finally:
        stop.set()
    message = str(excinfo.value)
    assert "stalled" in message, message
    # It must NOT be the wall-clock ceiling that saved us.
    assert "timed out after" not in message, message


def test_slow_but_progressing_remote_is_not_aborted(repo, tmp_path, tight_stall):
    """The discriminator: bytes still moving => never a stall, however slow the transfer.

    The remote dribbles above the floor for many multiples of the abort window. Any
    implementation keyed on elapsed time rather than throughput fails here."""
    port, stop = _serve("dribble", seconds=8, rate=4000)
    try:
        with pytest.raises(git_fetch.SnapshotFetchError) as excinfo:
            git_fetch.fetch_origin(
                repo,
                lock_path=tmp_path / "locks" / "fetch.lock",
                remote=f"http://127.0.0.1:{port}/x.git",
            )
    finally:
        stop.set()
    # It still fails (the remote is not a real git server) — but never as a stall.
    message = str(excinfo.value)
    assert "stalled" not in message, message
    assert not git_fetch.is_stall_abort(message), message


def test_fetch_argv_carries_the_low_speed_options(repo, tmp_path, monkeypatch):
    """Every fetch launched here hands git the throughput-keyed abort."""
    seen: list[list[str]] = []
    real_run = subprocess.run

    def fake_run(argv, **kwargs):
        # Only intercept the fetch: git_fetch.subprocess IS the stdlib module, so a blanket
        # fake would also swallow the harness's own git calls.
        if "fetch" not in argv:
            return real_run(argv, **kwargs)
        seen.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(git_fetch.subprocess, "run", fake_run)
    git_fetch.fetch_origin(repo, lock_path=tmp_path / "locks" / "fetch.lock")

    assert seen, "no git child was launched"
    argv = seen[0]
    assert "http.lowSpeedLimit=1000" in argv, argv
    assert "http.lowSpeedTime=10" in argv, argv


def test_only_a_stall_is_retried(repo, tmp_path, monkeypatch):
    """A stall is transient so it is retried; any other failure fails on the first try."""
    monkeypatch.setenv("REBAR_SNAPSHOT_STALL_ATTEMPTS", "3")
    calls: list[str] = []

    real_run = subprocess.run

    def make_run(stderr: str):
        def fake_run(argv, **kwargs):
            # See above: leave every non-fetch git child to the real subprocess.run.
            if "fetch" not in argv:
                return real_run(argv, **kwargs)
            calls.append(stderr)
            return subprocess.CompletedProcess(argv, 128, "", stderr)

        return fake_run

    stall = "fatal: Operation too slow. Less than 1000 bytes/sec transferred the last 10 seconds"
    monkeypatch.setattr(git_fetch.subprocess, "run", make_run(stall))
    with pytest.raises(git_fetch.SnapshotFetchError):
        git_fetch.fetch_origin(repo, lock_path=tmp_path / "locks" / "fetch.lock")
    assert len(calls) == 3, calls

    calls.clear()
    monkeypatch.setattr(git_fetch.subprocess, "run", make_run("fatal: Authentication failed"))
    with pytest.raises(git_fetch.SnapshotFetchError):
        git_fetch.fetch_origin(repo, lock_path=tmp_path / "locks" / "fetch.lock")
    assert len(calls) == 1, calls
