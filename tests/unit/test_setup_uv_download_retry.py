"""The local setup-uv action must survive a transient 5xx from GitHub's release download.

Bug sad-convective-bagworm: GitHub answered the anonymous ``releases/download`` request with
``504 Gateway Time-out`` for some jobs while others fetched the same asset in the same minute,
and the action's single ``Invoke-WebRequest`` had no retry, so every such job failed. These
tests pin a bounded retry on that download and, where ``pwsh`` is installed (GitHub's hosted
runners ship it), run the action's own download line against a local server.
"""

from __future__ import annotations

import http.server
import re
import shutil
import subprocess
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
import yaml
from _subprocess_env import subprocess_env

ACTION = Path(__file__).resolve().parents[2] / ".github" / "actions" / "setup-uv" / "action.yml"
PAYLOAD = b"uv-release-archive-bytes"
_PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")

needs_pwsh = pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh is not installed")


def _download_line() -> str:
    """The action's ``Invoke-WebRequest`` line, verbatim, from the step that installs uv."""
    action = yaml.safe_load(ACTION.read_text(encoding="utf-8"))
    for step in action["runs"]["steps"]:
        for line in str(step.get("run", "")).splitlines():
            if line.strip().startswith("Invoke-WebRequest"):
                return line.strip()
    raise AssertionError(f"no Invoke-WebRequest line in {ACTION}")


def test_setup_uv_download_retries_transient_failures() -> None:
    line = _download_line()
    count = re.search(r"-MaximumRetryCount\s+(\d+)\b", line)
    interval = re.search(r"-RetryIntervalSec\s+(\d+)\b", line)
    assert count is not None, line
    assert int(count.group(1)) >= 3, line
    assert interval is not None, line
    assert int(interval.group(1)) >= 1, line


class _Server:
    """A loopback HTTP server that answers the first ``failures`` GETs with 504."""

    def __init__(self, failures: int) -> None:
        self.requests = 0
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                owner.requests += 1
                if owner.requests <= failures:
                    self.send_response(504, "Gateway Time-out")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(PAYLOAD)))
                self.end_headers()
                self.wfile.write(PAYLOAD)

            def log_message(self, format: str, *args: object) -> None:
                return

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/uv.tar.gz"


@pytest.fixture
def serve() -> Iterator[Callable[[int], _Server]]:
    started: list[tuple[_Server, threading.Thread]] = []

    def start(failures: int) -> _Server:
        server = _Server(failures)
        thread = threading.Thread(target=server.httpd.serve_forever, daemon=True)
        thread.start()
        started.append((server, thread))
        return server

    yield start
    for server, thread in started:
        server.httpd.shutdown()
        server.httpd.server_close()
        thread.join(timeout=10)


def _run_download(
    line: str, url: str, archive: Path, tmp_path: Path
) -> subprocess.CompletedProcess[str]:
    script = tmp_path / "download.ps1"
    script.write_text(
        f"$ErrorActionPreference = 'Stop'\n$url = '{url}'\n$archive = '{archive}'\n{line}\n",
        encoding="utf-8",
    )
    env = subprocess_env()
    for name in _PROXY_VARS:
        env.pop(name, None)
    return subprocess.run(
        ["pwsh", "-NoProfile", "-NonInteractive", "-File", str(script)],
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
        check=False,
    )


@needs_pwsh
def test_setup_uv_download_survives_two_504s(
    serve: Callable[[int], _Server], tmp_path: Path
) -> None:
    server = serve(2)
    archive = tmp_path / "uv.tar.gz"
    result = _run_download(_download_line(), server.url, archive, tmp_path)
    assert result.returncode == 0, result.stderr
    assert archive.read_bytes() == PAYLOAD
    assert server.requests == 3


@needs_pwsh
def test_setup_uv_download_fails_on_persistent_504(
    serve: Callable[[int], _Server], tmp_path: Path
) -> None:
    server = serve(10_000)
    archive = tmp_path / "uv.tar.gz"
    line = re.sub(r"-RetryIntervalSec\s+\d+", "-RetryIntervalSec 1", _download_line())
    result = _run_download(line, server.url, archive, tmp_path)
    assert result.returncode != 0
    assert "504" in result.stderr + result.stdout
    assert not archive.exists()
