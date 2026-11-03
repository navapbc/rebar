"""The MCP server syncs its tickets clone on an idle timer and logs the outcome at WARNING.

Bug topnotch-brazen-slug: on 2026-10-18 the production clone stayed forked from the shared
branch for about 50 minutes after the deploy overlap ended, because only a client write ran the
push-recovery path, and nothing on the host showed it: push contention logs at DEBUG and a busy
lock skipped the sync silently. Each test below drives the real sync against a temp store with
a real bare remote, and reads the records at WARNING -- the level the server's stderr handler
shows by default.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from rebar._store import push_state, sync

pytestmark = pytest.mark.unit

_REJECT_HOOK = """\
#!/bin/sh
echo "remote: error: GH013: Repository rule violations found for refs/heads/tickets." >&2
exit 1
"""

_HOLDER_SRC = """
import sys, time
from rebar._store import lock
with lock.write_lock(sys.argv[1], timeout=10, attempts=1):
    print("holding", flush=True)
    time.sleep(60)
"""


def _git(d: Path, *a: str) -> subprocess.CompletedProcess[str]:
    r = subprocess.run(["git", "-C", str(d), *a], capture_output=True, text=True, check=False)
    assert r.returncode == 0, f"git {' '.join(a)} failed: {r.stderr}"
    return r


def _repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "tickets")
    _git(path, "config", "user.email", "t@e.com")
    _git(path, "config", "user.name", "T")
    _git(path, "config", "gc.auto", "0")
    return path


def _commit(repo: Path, rel: str, body: str = "{}\n") -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    _git(repo, "add", rel)
    _git(repo, "commit", "-q", "--no-verify", "-m", f"ticket: {rel}")


def _remote_head(origin: Path) -> str:
    return subprocess.run(
        ["git", "--git-dir", str(origin), "rev-parse", "refs/heads/tickets"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """A tracker one commit ahead of its published bare ``origin``."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True, capture_output=True)
    tracker = _repo(tmp_path / "tracker")
    _git(tracker, "remote", "add", "origin", str(origin))
    _commit(tracker, "aaaa-bbbb-cccc-dddd/1-x-CREATE.json")
    _git(tracker, "push", "-q", "origin", "HEAD:tickets")
    _git(tracker, "fetch", "-q", "origin", "+refs/heads/tickets:refs/remotes/origin/tickets")
    _commit(tracker, "aaaa-bbbb-cccc-dddd/2-y-COMMENT.json")
    monkeypatch.setenv("REBAR_SYNC_PUSH", "always")
    return {"origin": origin, "tracker": tracker}


def _warnings(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join(r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)


def test_the_idle_timer_delivers_an_unpushed_commit_with_no_client_write(
    store: dict[str, Path], caplog: pytest.LogCaptureFixture
) -> None:
    from rebar._mcp_health import InFlightGauge, run_idle_sync_loop

    tracker, origin = store["tracker"], store["origin"]
    head = _git(tracker, "rev-parse", "HEAD").stdout.strip()
    push_state.record_failure(str(tracker), "final-push-rejected", "earlier", "origin/tickets")
    stop = threading.Event()
    gauge = InFlightGauge()
    loop = threading.Thread(
        target=run_idle_sync_loop,
        args=(str(tracker), gauge),
        kwargs={"interval": 0.05, "stop": stop},
        daemon=True,
    )
    with caplog.at_level(logging.WARNING):
        loop.start()
        try:
            deadline = time.monotonic() + 30
            while _remote_head(origin) != head and time.monotonic() < deadline:
                time.sleep(0.05)
        finally:
            stop.set()
            loop.join(timeout=30)

    assert _remote_head(origin) == head, "the idle timer never published the local commit"
    assert "delivered" in _warnings(caplog)
    assert push_state.read_status(str(tracker))["state"] == "ok"
    assert gauge.value == 0, "an idle-sync run left the drain gauge held"


def test_a_rejected_push_logs_push_pending_at_warning(
    store: dict[str, Path], caplog: pytest.LogCaptureFixture
) -> None:
    origin = store["origin"]
    hook = origin / "hooks" / "pre-receive"
    hook.write_text(_REJECT_HOOK)
    hook.chmod(0o755)

    with caplog.at_level(logging.WARNING):
        outcome = sync.idle_sync(str(store["tracker"]))

    assert outcome == "pending"
    assert "PUSH_PENDING" in _warnings(caplog)


def test_an_unrelated_remote_history_logs_diverged_at_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True, capture_output=True)
    other = _repo(tmp_path / "other")
    _commit(other, "ffff-eeee-dddd-cccc/1-z-CREATE.json")
    _git(other, "push", "-q", str(origin), "HEAD:tickets")
    tracker = _repo(tmp_path / "tracker")
    _git(tracker, "remote", "add", "origin", str(origin))
    _commit(tracker, "aaaa-bbbb-cccc-dddd/1-x-CREATE.json")
    monkeypatch.setenv("REBAR_SYNC_PUSH", "always")

    with caplog.at_level(logging.WARNING):
        outcome = sync.idle_sync(str(tracker))

    assert outcome == "diverged"
    assert "DIVERGED" in _warnings(caplog)


def test_a_busy_write_lock_is_logged_with_its_holder(
    store: dict[str, Path], caplog: pytest.LogCaptureFixture
) -> None:
    tracker = str(store["tracker"])
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLDER_SRC, tracker],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "holding"
        with caplog.at_level(logging.WARNING):
            outcome = sync.idle_sync(tracker, lock_timeout=1)
    finally:
        holder.kill()
        holder.wait(timeout=30)

    assert outcome == "lock-busy"
    text = _warnings(caplog)
    assert "lock busy" in text
    assert f"pid={holder.pid}" in text


def test_an_already_current_store_logs_nothing_at_warning(
    store: dict[str, Path], caplog: pytest.LogCaptureFixture
) -> None:
    tracker = store["tracker"]
    _git(tracker, "push", "-q", "origin", "HEAD:tickets")

    with caplog.at_level(logging.WARNING):
        outcome = sync.idle_sync(str(tracker))

    assert outcome == "current"
    assert _warnings(caplog) == ""


def test_a_failing_run_is_logged_and_the_next_tick_still_runs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from rebar._mcp_health import InFlightGauge, run_idle_sync_loop

    calls: list[str] = []
    stop = threading.Event()

    def flaky(tracker: str) -> str:
        calls.append(tracker)
        if len(calls) == 1:
            raise OSError("disk went away")
        stop.set()
        return "current"

    with caplog.at_level(logging.WARNING):
        run_idle_sync_loop(str(tmp_path), InFlightGauge(), interval=0.01, stop=stop, sync_fn=flaky)

    assert len(calls) == 2, "the loop died with the first failure"
    failed = [r for r in caplog.records if "idle tickets sync failed" in r.getMessage()]
    assert failed and failed[0].exc_info is not None


def test_the_loop_stops_once_the_server_is_draining(tmp_path: Path) -> None:
    from rebar._mcp_health import InFlightGauge, run_idle_sync_loop

    gauge = InFlightGauge()
    gauge.begin_draining()
    calls: list[str] = []

    run_idle_sync_loop(
        str(tmp_path), gauge, interval=0.01, stop=threading.Event(), sync_fn=calls.append
    )

    assert calls == []


def test_start_idle_sync_runs_on_a_context_daemon_without_holding_the_gauge(
    store: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from rebar import _mcp_health

    tracker = store["tracker"]
    stop = threading.Event()
    real_loop = _mcp_health.run_idle_sync_loop
    monkeypatch.setattr("rebar.config.tracker_dir", lambda *_a, **_k: tracker)
    monkeypatch.setattr(
        _mcp_health,
        "run_idle_sync_loop",
        lambda t, g: real_loop(t, g, interval=0.01, stop=stop, sync_fn=lambda _t: stop.set()),
    )
    gauge = _mcp_health.InFlightGauge()

    thread = _mcp_health.start_idle_sync(gauge)

    assert thread is not None and thread.daemon and thread.name == "rebar-mcp-idle-sync"
    thread.join(timeout=30)
    assert stop.is_set() and not thread.is_alive()
    assert gauge.value == 0


def test_start_idle_sync_does_nothing_without_a_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rebar import _mcp_health

    monkeypatch.setattr("rebar.config.tracker_dir", lambda *_a, **_k: tmp_path / "absent")

    assert _mcp_health.start_idle_sync(_mcp_health.InFlightGauge()) is None


def test_a_removed_input_stops_the_loop_instead_of_retrying(tmp_path: Path) -> None:
    from rebar._deprecations import RemovedInputError
    from rebar._mcp_health import InFlightGauge, run_idle_sync_loop

    calls: list[str] = []

    def removed(tracker: str) -> str:
        calls.append(tracker)
        raise RemovedInputError("a retired input is still set")

    gauge = InFlightGauge()
    with pytest.raises(RemovedInputError):
        run_idle_sync_loop(
            str(tmp_path), gauge, interval=0.01, stop=threading.Event(), sync_fn=removed
        )

    assert len(calls) == 1
    assert gauge.value == 0


@pytest.mark.parametrize(("transport", "started"), [("http", True), ("stdio", False)])
def test_main_starts_the_idle_sync_only_for_the_served_transport(
    transport: str, started: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import rebar._logging
    import rebar._mcp_health as health
    from rebar import mcp_server

    store = tmp_path / "store"
    store.mkdir()
    monkeypatch.setenv("REBAR_TRACKER_DIR", str(store))
    monkeypatch.setenv("REBAR_MCP_TRANSPORT", transport)
    monkeypatch.setenv("REBAR_MCP_ALLOW_UNAUTHENTICATED_HTTP", "1")  # loopback test server
    monkeypatch.setattr(sys, "argv", ["rebar-mcp"])
    monkeypatch.setattr(rebar._logging, "install_stderr_handler", lambda _name: None)
    monkeypatch.setattr(mcp_server, "compose_startup_opcert_binding", lambda _cfg: None)
    monkeypatch.setattr(health, "start_startup_store_sweep", lambda _gauge: None)
    calls: list[object] = []
    monkeypatch.setattr(health, "start_idle_sync", calls.append)
    monkeypatch.setattr(health, "run_mcp", lambda *_a, **_k: None)

    mcp_server.main()

    assert bool(calls) is started
