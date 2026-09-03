from __future__ import annotations

import contextlib
import http.server
import re
import shlex
import socket
import subprocess
import threading
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

_LOOPBACK_HEALTH_URL = re.compile(r"http://127\.0\.0\.1:(\d+)(/health\b[^\s'\"()]*)")


def _logical_dockerfile_lines(text: str) -> list[str]:
    lines: list[str] = []
    current = ""
    for raw_line in text.splitlines():
        stripped = raw_line.rstrip()
        if not current:
            current = stripped
        else:
            current += stripped.lstrip()
        if stripped.endswith("\\"):
            current = current[:-1] + " "
            continue
        if current:
            lines.append(current.strip())
        current = ""
    if current:
        lines.append(current.strip())
    return lines


def final_stage_healthcheck_argv(text: str) -> list[str]:
    """Return the final Docker stage HEALTHCHECK argv."""
    stages: list[list[str]] = []
    current: list[str] = []
    for line in _logical_dockerfile_lines(text):
        if line.startswith("FROM "):
            if current:
                stages.append(current)
            current = [line]
            continue
        if current:
            current.append(line)
    if current:
        stages.append(current)
    assert stages, "Dockerfile must define at least one stage"
    final_stage = stages[-1]
    healthcheck = next((line for line in final_stage if line.startswith("HEALTHCHECK ")), None)
    assert healthcheck is not None, "final Docker stage must declare a HEALTHCHECK"
    marker = " CMD "
    assert marker in healthcheck, (
        f"only HEALTHCHECK CMD forms are supported by this oracle; got {healthcheck!r}"
    )
    return shlex.split(healthcheck.split(marker, 1)[1])


def healthcheck_test_argv(test_value: Sequence[str] | str) -> list[str]:
    """Return the executable argv from a compose healthcheck test value."""
    if isinstance(test_value, str):
        return shlex.split(test_value)
    parts = [str(part) for part in test_value]
    assert parts, "healthcheck test must not be empty"
    assert parts[0] == "CMD", f"expected healthcheck to start with CMD, got {parts[0]!r}"
    return parts[1:]


def _rewrite_health_url(argv: Sequence[str], port: int) -> list[str]:
    rewritten: list[str] = []
    for arg in argv:
        rewritten.append(
            _LOOPBACK_HEALTH_URL.sub(
                lambda match: f"http://127.0.0.1:{port}{match.group(2)}",
                arg,
            )
        )
    assert rewritten != list(argv), (
        f"expected a loopback /health URL to rewrite in healthcheck argv {argv!r}"
    )
    return rewritten


def _reserve_closed_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextlib.contextmanager
def _healthy_listener() -> Iterator[int]:
    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *_args) -> None:
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@contextlib.contextmanager
def _malformed_listener() -> Iterator[int]:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = int(server.getsockname()[1])
    ready = threading.Event()

    def _serve() -> None:
        ready.set()
        try:
            conn, _addr = server.accept()
        except OSError:
            return
        with conn:
            try:
                conn.sendall(b"this is not http\r\n\r\n")
            except OSError:
                return

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    ready.wait(timeout=2)
    try:
        yield port
    finally:
        server.close()
        thread.join(timeout=2)


@dataclass(frozen=True)
class HealthcheckOutcomes:
    success: subprocess.CompletedProcess[str]
    closed: subprocess.CompletedProcess[str]
    malformed: subprocess.CompletedProcess[str]


def _run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv),
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )


def exercise_socket_healthcheck(argv: Sequence[str]) -> HealthcheckOutcomes:
    """Run a healthcheck against healthy, closed, and malformed listeners."""
    with _healthy_listener() as healthy_port, _malformed_listener() as malformed_port:
        success = _run(_rewrite_health_url(argv, healthy_port))
        malformed = _run(_rewrite_health_url(argv, malformed_port))
    closed = _run(_rewrite_health_url(argv, _reserve_closed_port()))
    return HealthcheckOutcomes(success=success, closed=closed, malformed=malformed)


def assert_socket_healthcheck_semantics(argv: Sequence[str]) -> None:
    outcomes = exercise_socket_healthcheck(argv)
    assert outcomes.success.returncode == 0, (
        "healthcheck must succeed against a healthy loopback listener; "
        f"stdout={outcomes.success.stdout!r} stderr={outcomes.success.stderr!r}"
    )
    assert outcomes.closed.returncode != 0, (
        "healthcheck must fail against a closed listener; a token-only or print-only "
        "command is not a real liveness probe"
    )
    assert outcomes.malformed.returncode != 0, (
        "healthcheck must fail when the listener does not return a valid HTTP response"
    )
