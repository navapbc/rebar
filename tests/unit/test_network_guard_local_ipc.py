"""Block network sockets while allowing local IPC in unit/scripts tests (0b31).

The autouse guard rejects AF_INET and AF_INET6. It must leave AF_UNIX alone so
multiprocessing forkserver on Python 3.14+ can reach its local socket, and it must let
the stdlib socketpair fallback connect its in-process loopback pair, which is how every
asyncio event loop gets its self-pipe on Windows (fleece-grave-noolbenger)."""

from __future__ import annotations

import asyncio
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


def test_network_guard_allows_the_stdlib_loopback_socketpair() -> None:
    """The stdlib socketpair fallback is in-process IPC, so the guard must allow it.

    Windows has no ``AF_UNIX`` socketpair, so ``socket.socketpair`` there is
    ``socket._fallback_socketpair``: it connects an ``AF_INET`` loopback pair inside the
    process. Calling it directly runs that same connect on every platform.
    """
    left, right = socket._fallback_socketpair()  # type: ignore[attr-defined]
    try:
        left.sendall(b"ping")
        assert right.recv(4) == b"ping"
        right.sendall(b"pong")
        assert left.recv(4) == b"pong"
    finally:
        left.close()
        right.close()


def test_network_guard_allows_an_event_loop_on_the_windows_socketpair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Report replication: ``asyncio.run`` must work where socketpair is the fallback.

    Every new event loop builds its self-pipe with ``socket.socketpair()``. Installing the
    fallback reproduces the Windows sweep, where the guard failed ``asyncio.run`` (and every
    TestClient lifespan) before any test code ran.
    """
    monkeypatch.setattr(socket, "socketpair", socket._fallback_socketpair)  # type: ignore[attr-defined]

    async def _answer() -> int:
        await asyncio.sleep(0)
        return 42

    assert asyncio.run(_answer()) == 42


def test_network_guard_still_blocks_a_loopback_connect_from_test_code() -> None:
    """Teeth: the exemption is the stdlib socketpair only, not every loopback address.

    The listener is in this process, so without the guard the connect would succeed; the
    guard must still refuse it because the caller is not the stdlib socketpair.
    """
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        with pytest.raises(RuntimeError, match="Network access is forbidden"):
            client.connect(server.getsockname())
    finally:
        client.close()
        server.close()


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
