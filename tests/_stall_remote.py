"""A local fake git remote that either goes silent or dribbles — for stall-abort tests.

Extracted from ``tests/unit/test_snapshot_fetch_stall_abort_12e4.py`` when task ``851e``
armed the same throughput-keyed abort on the other 300s network fetches, so every site's
test drives the SAME two remotes rather than growing its own near-copy.

Both modes speak just enough HTTP for git's curl transport to start a body transfer:
``stall`` sends the response headers and then **zero** body bytes, ``dribble`` sends body
bytes continuously at a fixed rate. The pair is what makes an assertion throughput-keyed
rather than wall-clock keyed — an implementation that merely lowers a timeout aborts both,
and the dribble case fails it.
"""

from __future__ import annotations

import socket
import threading
import time


def serve(mode: str, *, seconds: float, rate: int = 0) -> tuple[int, threading.Event]:
    """Bind a local HTTP-ish remote and return its port plus a shutdown flag.

    ``mode="stall"`` sends response headers and then **zero** body bytes for ``seconds``.
    ``mode="dribble"`` sends body bytes continuously at ``rate`` bytes/second — slow, but
    always progressing. The caller MUST ``stop.set()`` (in a ``finally``) to release the
    listener thread."""
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
