"""Real-Git oracle for blobless tickets-branch CI fetches (B1 037b)."""

from __future__ import annotations

import os
import re
import socket
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml
from _subprocess_env import subprocess_env

import rebar

pytestmark = pytest.mark.integration

_ROOT = Path(__file__).resolve().parents[2]
_HISTORICAL_BYTES = 141 * 1024 * 1024
_SMALL_HISTORICAL_BYTES = 1024
_SMALL_PACK_LIMIT_KIB = 1024
_STANDALONE_LIMIT = 102400


def _run(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=180)
    if check and result.returncode:
        detail = (
            f"command failed ({result.returncode}): {argv}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        pytest.fail(detail)
    return result


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    return _run(["git", *args], cwd=repo, env=env).stdout.strip()


def _write_incompressible(path: Path, byte_count: int) -> None:
    remaining = byte_count
    with path.open("wb") as stream:
        while remaining:
            chunk = os.urandom(min(1024 * 1024, remaining))
            stream.write(chunk)
            remaining -= len(chunk)


@dataclass(frozen=True)
class TicketServer:
    bare: Path
    url: str
    tip: str
    historical_blob: str


def _ticket_server(
    tmp_path_factory: pytest.TempPathFactory,
    *,
    fixture_name: str,
    historical_bytes: int,
) -> Iterator[TicketServer]:
    root = tmp_path_factory.mktemp(fixture_name)
    source = root / "source"
    source.mkdir()
    _git(source, "init", "--quiet", "--initial-branch=main")
    _git(source, "config", "user.email", "ci@example.com")
    _git(source, "config", "user.name", "CI fixture")
    _git(source, "config", "commit.gpgsign", "false")
    (source / "README.md").write_text("code checkout\n", encoding="utf-8")
    _git(source, "add", "README.md")
    _git(source, "commit", "--quiet", "-m", "seed code")

    rebar.init_repo(repo_root=str(source))
    tracker = source / ".tickets-tracker"
    assert tracker.is_dir(), "rebar init must materialize the tickets worktree"
    _git(tracker, "config", "user.email", "ci@example.com")
    _git(tracker, "config", "user.name", "CI fixture")
    large = tracker / "historical.bin"
    _write_incompressible(large, historical_bytes)
    _git(tracker, "add", "historical.bin")
    _git(tracker, "commit", "--quiet", "-m", "large historical payload")
    historical_blob = _git(tracker, "rev-parse", "HEAD:historical.bin")
    large.unlink()
    (tracker / "tip.txt").write_text("small current tip\n", encoding="utf-8")
    (tracker / ".bridge_state").mkdir(exist_ok=True)
    (tracker / ".bridge_state" / "cursor").write_text("base\n", encoding="utf-8")
    _git(tracker, "add", "-A")
    _git(tracker, "commit", "--quiet", "-m", "small current tip")
    tip = _git(tracker, "rev-parse", "HEAD")

    served = root / "served"
    served.mkdir()
    bare = served / "tickets.git"
    _run(["git", "clone", "--quiet", "--bare", str(source), str(bare)], cwd=root)
    _git(bare, "fetch", str(source), "+tickets:tickets")
    _git(bare, "repack", "-a", "-d")
    _git(bare, "prune", "--expire=now")

    global_config = root / "daemon.gitconfig"
    global_config.write_text("[uploadpack]\n\tallowFilter = true\n", encoding="utf-8")
    daemon_env = subprocess_env(
        {
            "GIT_CONFIG_GLOBAL": str(global_config),
            "GIT_CONFIG_NOSYSTEM": "1",
        }
    )
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    daemon = subprocess.Popen(
        [
            "git",
            "daemon",
            "--reuseaddr",
            "--listen=127.0.0.1",
            f"--port={port}",
            "--export-all",
            "--enable=upload-pack",
            f"--base-path={served}",
            str(served),
        ],
        cwd=root,
        env=daemon_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 30
    while True:
        if daemon.poll() is not None:
            stdout, stderr = daemon.communicate()
            pytest.fail(f"git daemon exited during startup:\n{stdout}\n{stderr}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            if time.monotonic() >= deadline:
                daemon.terminate()
                daemon.wait(timeout=5)
                pytest.fail("git daemon did not accept connections within 30 seconds")
            time.sleep(0.1)
    try:
        yield TicketServer(bare, f"git://127.0.0.1:{port}/tickets.git", tip, historical_blob)
    finally:
        daemon.terminate()
        try:
            daemon.wait(timeout=5)
        except subprocess.TimeoutExpired:
            daemon.kill()
            daemon.wait()


@pytest.fixture(scope="module")
def ticket_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[TicketServer]:
    yield from _ticket_server(
        tmp_path_factory,
        fixture_name="tickets-partial-clone",
        historical_bytes=_HISTORICAL_BYTES,
    )


@pytest.fixture(scope="module")
def small_ticket_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[TicketServer]:
    yield from _ticket_server(
        tmp_path_factory,
        fixture_name="tickets-partial-clone-small",
        historical_bytes=_SMALL_HISTORICAL_BYTES,
    )


def _init_client(path: Path) -> None:
    path.mkdir()
    _git(path, "init", "--quiet", "--initial-branch=main")
    _git(path, "config", "user.email", "ci@example.com")
    _git(path, "config", "user.name", "CI fixture")
    _git(path, "config", "commit.gpgsign", "false")
    _git(path, "commit", "--quiet", "--allow-empty", "-m", "code checkout")


def _fetch(
    path: Path,
    server: TicketServer,
    *options: str,
    named_origin: bool = False,
    named_filter: str | None = None,
) -> Path:
    _init_client(path)
    if named_origin:
        _git(path, "remote", "add", "origin", server.url)
        if named_filter is not None:
            _git(path, "config", "remote.origin.promisor", "true")
            _git(path, "config", "remote.origin.partialclonefilter", named_filter)
        source = "origin"
    else:
        source = server.url
    trace = path.parent / f"{path.name}.packet"
    env = subprocess_env({"GIT_TRACE_PACKET": str(trace), "GIT_TERMINAL_PROMPT": "0"})
    _git(path, "fetch", *options, source, "+tickets:refs/remotes/origin/tickets", env=env)
    return trace


def _size_pack_kib(repo: Path) -> int:
    _git(repo, "repack", "-a", "-d")
    fields = dict(
        line.split(": ", 1)
        for line in _git(repo, "count-objects", "-v").splitlines()
        if ": " in line
    )
    return int(fields["size-pack"])


def _standalone_checkout_filter() -> str | None:
    workflow = yaml.safe_load(
        (_ROOT / ".github/workflows/verify-identity.yml").read_text(encoding="utf-8")
    )
    checkout = next(
        step
        for step in workflow["jobs"]["verify-identity"]["steps"]
        if step.get("uses") == "actions/checkout@v7"
    )
    return (checkout.get("with") or {}).get("filter")


def _gerrit_fetch_options(job: str) -> tuple[str, ...]:
    workflow = yaml.safe_load(
        (_ROOT / ".github/workflows/gerrit-verify.yaml").read_text(encoding="utf-8")
    )
    script = next(
        str(step["run"])
        for step in workflow["jobs"][job]["steps"]
        if "git fetch" in str(step.get("run", "")) and "tickets" in str(step.get("run", ""))
    )
    options: list[str] = []
    depth = re.search(r"--depth(?:=|\s+)(\d+)", script)
    if depth:
        options.append(f"--depth={depth.group(1)}")
    partial_filter = re.search(r"--filter=([^\s\\]+)", script)
    if partial_filter:
        options.append(f"--filter={partial_filter.group(1)}")
    return tuple(options)


def _assert_filter_protocol(trace: Path) -> None:
    packet = trace.read_text(encoding="utf-8", errors="replace")
    assert re.search(r"packet:\s+\S+<\s+.*\bfilter\b", packet), packet
    assert re.search(r"packet:\s+\S+>\s+filter blob:none$", packet, re.MULTILINE), packet


def _assert_historical_blob_missing(repo: Path, server: TicketServer) -> None:
    env = subprocess_env({"GIT_NO_LAZY_FETCH": "1"})
    result = _run(["git", "cat-file", "-e", server.historical_blob], cwd=repo, env=env, check=False)
    assert result.returncode != 0, "historical payload unexpectedly exists in blobless client"


def _guard_script(path: Path, job: str) -> str:
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    matches = [
        str(step.get("run", ""))
        for step in workflow["jobs"][job]["steps"]
        if "git count-objects -v" in str(step.get("run", ""))
        and "rebar verify-identity" not in str(step.get("run", ""))
    ]
    assert len(matches) == 1
    return matches[0]


def _run_guard(repo: Path, workflow: Path, job: str, env_name: str, limit: int) -> int:
    env = subprocess_env({env_name: str(limit)})
    result = _run(["bash", "-c", _guard_script(workflow, job)], cwd=repo, env=env, check=False)
    return result.returncode


def test_named_origin_filter_is_negotiated_and_omits_blob(
    tmp_path: Path, small_ticket_server: TicketServer
) -> None:
    client = tmp_path / "named"
    trace = _fetch(
        client,
        small_ticket_server,
        named_origin=True,
        named_filter=_standalone_checkout_filter(),
    )
    _assert_filter_protocol(trace)
    _assert_historical_blob_missing(client, small_ticket_server)


def test_direct_depth_filter_is_negotiated_and_omits_blob(
    tmp_path: Path, small_ticket_server: TicketServer
) -> None:
    client = tmp_path / "depth"
    trace = _fetch(client, small_ticket_server, *_gerrit_fetch_options("require-ticket"))
    _assert_filter_protocol(trace)
    _assert_historical_blob_missing(client, small_ticket_server)


def test_direct_full_filter_is_negotiated_and_omits_blob(
    tmp_path: Path, small_ticket_server: TicketServer
) -> None:
    client = tmp_path / "full-filtered"
    trace = _fetch(client, small_ticket_server, *_gerrit_fetch_options("verify-identity"))
    _assert_filter_protocol(trace)
    _assert_historical_blob_missing(client, small_ticket_server)


def test_small_server_full_fetch_keeps_historical_blob_below_one_mib(
    tmp_path: Path, small_ticket_server: TicketServer
) -> None:
    client = tmp_path / "small-full"
    _fetch(client, small_ticket_server)
    historical = _run(
        ["git", "cat-file", "-e", small_ticket_server.historical_blob],
        cwd=client,
        check=False,
    )
    assert historical.returncode == 0, "full fetch must include the small historical blob"
    assert _size_pack_kib(client) < _SMALL_PACK_LIMIT_KIB


@pytest.mark.xdist_group("large_ticket_server")
def test_standalone_guard_rejects_full(tmp_path: Path, ticket_server: TicketServer) -> None:
    client = tmp_path / "standalone-full"
    _fetch(client, ticket_server)
    assert _size_pack_kib(client) > _STANDALONE_LIMIT
    workflow = _ROOT / ".github/workflows/verify-identity.yml"
    assert (
        _run_guard(
            client, workflow, "verify-identity", "REBAR_CHECKOUT_PACK_LIMIT_KIB", _STANDALONE_LIMIT
        )
        != 0
    )


@pytest.mark.xdist_group("large_ticket_server")
def test_standalone_guard_accepts_blobless(tmp_path: Path, ticket_server: TicketServer) -> None:
    client = tmp_path / "standalone-blobless"
    _fetch(client, ticket_server, "--filter=blob:none")
    assert _size_pack_kib(client) < _STANDALONE_LIMIT
    workflow = _ROOT / ".github/workflows/verify-identity.yml"
    assert (
        _run_guard(
            client, workflow, "verify-identity", "REBAR_CHECKOUT_PACK_LIMIT_KIB", _STANDALONE_LIMIT
        )
        == 0
    )


def test_gerrit_verify_has_no_standalone_pack_guard() -> None:
    """The gerrit-verify tickets-pack guard was removed deliberately (a092).

    The `tickets` branch is append-only full-history (ADR 0051), so its pack only
    grows and any fixed KiB ceiling is guaranteed to trip and block every Gerrit
    change repo-wide. Pin the deliberate absence so the fail-closed guard is not
    silently reintroduced.
    """
    workflow = _ROOT / ".github/workflows/gerrit-verify.yaml"
    job_def = yaml.safe_load(workflow.read_text(encoding="utf-8"))["jobs"]["verify-identity"]
    assert "REBAR_TICKETS_PACK_LIMIT_KIB" not in (job_def.get("env") or {})
    standalone = [
        step
        for step in job_def["steps"]
        if "git count-objects -v" in str(step.get("run", ""))
        and "rebar verify-identity" not in str(step.get("run", ""))
    ]
    assert not standalone, "the gerrit-verify tickets-pack guard must stay removed (a092)"


@pytest.mark.parametrize("lane", ["standalone", "gerrit"])
def test_identity_no_promisor_fetch(
    tmp_path: Path, small_ticket_server: TicketServer, lane: str
) -> None:
    client = tmp_path / f"identity-{lane}"
    if lane == "standalone":
        _fetch(
            client,
            small_ticket_server,
            named_origin=True,
            named_filter=_standalone_checkout_filter(),
        )
    else:
        _fetch(client, small_ticket_server, *_gerrit_fetch_options("verify-identity"))
    _assert_historical_blob_missing(client, small_ticket_server)
    tracker = client / ".tickets-tracker"
    _git(client, "worktree", "add", "-B", "tickets", str(tracker), "origin/tickets")
    before = _git(client, "count-objects", "-v")
    env = subprocess_env(
        {
            "REBAR_ROOT": str(client),
            "REBAR_TRACKER_DIR": str(tracker),
            "REBAR_IDENTITY_REQUIRE_AUTHENTICATED": "0",
        }
    )
    result = _run(["rebar", "verify-identity"], cwd=client, env=env, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert _git(client, "count-objects", "-v") == before


def test_blobless_tickets_reconverges_and_pushes(
    tmp_path: Path, small_ticket_server: TicketServer
) -> None:
    client = tmp_path / "client"
    _fetch(
        client,
        small_ticket_server,
        named_origin=True,
        named_filter=_standalone_checkout_filter(),
    )
    _assert_historical_blob_missing(client, small_ticket_server)
    _git(client, "remote", "set-url", "--push", "origin", str(small_ticket_server.bare))
    tracker = client / ".tickets-tracker"
    _git(client, "worktree", "add", "-B", "tickets", str(tracker), "origin/tickets")
    _git(client, "config", "merge.ours.driver", "true")
    (tracker / "local-event.json").write_text('{"side":"local"}\n', encoding="utf-8")
    (tracker / ".bridge_state" / "cursor").write_text("local\n", encoding="utf-8")
    _git(tracker, "add", "-A")
    _git(tracker, "commit", "--quiet", "-m", "local event")

    remote = tmp_path / "remote-writer"
    _run(["git", "clone", "--quiet", str(small_ticket_server.bare), str(remote)], cwd=tmp_path)
    _git(remote, "config", "user.email", "ci@example.com")
    _git(remote, "config", "user.name", "CI fixture")
    _git(remote, "checkout", "--quiet", "tickets")
    (remote / "remote-event.json").write_text('{"side":"remote"}\n', encoding="utf-8")
    (remote / ".bridge_state" / "cursor").write_text("remote\n", encoding="utf-8")
    _git(remote, "add", "-A")
    _git(remote, "commit", "--quiet", "-m", "remote event")
    _git(remote, "push", "--quiet", "origin", "tickets")

    _git(tracker, "fetch", "origin", "+tickets:refs/remotes/origin/tickets")
    _git(tracker, "merge", "--no-edit", "origin/tickets")
    assert (tracker / "local-event.json").read_text(encoding="utf-8") == '{"side":"local"}\n'
    assert (tracker / "remote-event.json").read_text(encoding="utf-8") == '{"side":"remote"}\n'
    assert (tracker / ".bridge_state" / "cursor").read_text(encoding="utf-8") == "local\n"
    _git(tracker, "push", "origin", "HEAD:tickets")
    assert _git(small_ticket_server.bare, "rev-parse", "tickets") == _git(
        tracker, "rev-parse", "HEAD"
    )
