"""Block network sockets while allowing local IPC in unit/scripts tests (0b31).

The autouse guard rejects AF_INET and AF_INET6. It must leave AF_UNIX alone so
multiprocessing forkserver on Python 3.14+ can reach its local socket."""

from __future__ import annotations

import multiprocessing as mp
import socket
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def test_network_guard_allows_af_unix_connect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ``AF_UNIX`` connect is local IPC, never network, so the guard must allow it."""
    if not hasattr(socket, "AF_UNIX"):  # pragma: no cover - non-POSIX only
        pytest.skip("AF_UNIX unavailable on this platform")

    # Bind a short *relative* name (the sun_path limit is ~104 chars and pytest's
    # tmp_path is often longer) by anchoring cwd at tmp_path.
    monkeypatch.chdir(tmp_path)
    sock_path = "ipc.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(sock_path)
    server.listen(1)
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        # Under the over-broad guard this raises RuntimeError; the fix lets it through.
        client.connect(sock_path)
        conn, _ = server.accept()
        conn.close()
    finally:
        client.close()
        server.close()


def test_network_guard_still_blocks_real_tcp() -> None:
    """Teeth: the guard must keep rejecting a real AF_INET (TCP) connect after narrowing.

    Targets ``203.0.113.1`` (TEST-NET-3, RFC 5737) — reserved for documentation and never
    routable — so the guard, not a real dial-out, is what this asserts against.
    """
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(RuntimeError, match="Network access is forbidden"):
            client.connect(("203.0.113.1", 80))
    finally:
        client.close()


def _child(ready: mp.synchronize.Event, finish: mp.synchronize.Event) -> None:
    ready.set()
    finish.wait(30)


@pytest.mark.requires_posix_named_semaphore
def test_network_guard_allows_forkserver_multiprocessing() -> None:
    """Report replication: a ``forkserver`` child must start under the guard.

    ``forkserver`` is forced explicitly so this reproduces the py3.14 mechanism on every
    host (the CI core tiers default to ``fork``/``spawn``), not only where it is the
    default start method.
    """
    if "forkserver" not in mp.get_all_start_methods():  # pragma: no cover - platform dep
        pytest.skip("forkserver start method unavailable on this platform")

    ctx = mp.get_context("forkserver")
    ready = ctx.Event()
    finish = ctx.Event()
    proc = ctx.Process(target=_child, args=(ready, finish), daemon=True)
    proc.start()
    try:
        assert ready.wait(30), "forkserver child never started (guard blocked AF_UNIX IPC?)"
    finally:
        finish.set()
        proc.join(30)
