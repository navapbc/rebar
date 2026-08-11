"""Task ``851e``: the remaining 300s network fetches abort on THROUGHPUT, not the clock.

Bug ``12e4`` armed ``http.lowSpeedLimit``/``http.lowSpeedTime`` on the snapshot fetch path
after a stalled transfer burned the full 300s ``_GIT_TIMEOUT``. Three other network fetches
carried the identical exposure — a socket that opens and then moves no bytes looks exactly
like a legitimately slow cold clone, so only the wall-clock ceiling ends it:

* ``rebar.review_bot.gerrit_client.GerritClient.clone_change_ref`` (two fetches),
* ``rebar.opcert_service.workspace._git`` (the ``review`` and ``tickets`` fetches),
* ``rebar._commands.init._git_fetch`` (the ticket-branch fetch), armed at the seam it
  shares with the remote probe, ``_init_probe.run_bounded_git``.

Each site now splices ``git_fetch.stall_abort_args()`` before the subcommand — the same
seam, not a copy of it. Wrapper coverage proves that wiring and each wrapper's zero-byte
stall behavior. The shared snapshot boundary in 12e4 owns the single real Git/libcurl
above-floor integration that discriminates throughput aborts from elapsed-time aborts.
"""

from __future__ import annotations

import subprocess
from unittest import mock

import pytest
from _stall_remote import serve

from rebar._snapshot import git_fetch

# Shrink the abort window so a stalled fetch fails in seconds; the MECHANISM is unchanged.
_TIGHT_WINDOW = "2"


@pytest.fixture
def tight_stall(monkeypatch):
    monkeypatch.setenv("REBAR_SNAPSHOT_STALL_FLOOR_BYTES_PER_SEC", "1000")
    monkeypatch.setenv("REBAR_SNAPSHOT_STALL_WINDOW_SECONDS", _TIGHT_WINDOW)


def _fetch_argvs(seen: list[list[str]]) -> list[list[str]]:
    return [argv for argv in seen if "fetch" in argv]


def _assert_armed(argv: list[str]) -> None:
    """The two ``-c`` pairs are present AND precede the subcommand (git requires that)."""
    assert "http.lowSpeedLimit=1000" in argv, argv
    assert "http.lowSpeedTime=10" in argv, argv
    assert argv.index("http.lowSpeedTime=10") < argv.index("fetch"), argv


# ── site 1: the review bot's clone of the Gerrit change ──────────────────────


def _client(tmp_path, base_url: str = "http://gerrit.invalid:8080"):
    from rebar.review_bot.config import ReceiverConfig
    from rebar.review_bot.gerrit_client import GerritClient

    return GerritClient(
        ReceiverConfig(
            llm_review_max_value=1,
            llm_review_block_value=-1,
            dedup_db_path=str(tmp_path / "voted.db"),
            gerrit_bot_token="tok",
            webhook_token="tok",
            project="rebar",
            gerrit_base_url=base_url,
        )
    )


def test_gerrit_clone_arms_the_abort_on_every_fetch(tmp_path):
    """BOTH network fetches in ``clone_change_ref`` carry the low-speed options."""
    from rebar.review_bot import gerrit_client as gc_mod

    seen: list[list[str]] = []

    def _fake_run(cmd, **kwargs):
        seen.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with mock.patch.object(gc_mod.subprocess, "run", _fake_run):
        _client(tmp_path).clone_change_ref(42, "refs/changes/42/42/1", str(tmp_path / "dest"))

    fetches = _fetch_argvs(seen)
    assert len(fetches) == 2, fetches
    for argv in fetches:
        _assert_armed(argv)


def test_gerrit_clone_stalled_remote_aborts_as_a_stall(tmp_path, tight_stall):
    from rebar.review_bot.gerrit_client import GerritError

    port, stop = serve("stall", seconds=30)
    try:
        gc = _client(tmp_path, base_url=f"http://127.0.0.1:{port}")
        with pytest.raises(GerritError) as excinfo:
            gc.clone_change_ref(42, "refs/changes/42/42/1", str(tmp_path / "dest"))
    finally:
        stop.set()
    message = str(excinfo.value)
    assert git_fetch.is_stall_abort(message), message
    assert "timed out" not in message.lower(), message


# ── site 2: the op-cert service's authoritative-state workspace ──────────────


def _opcert_cfg(review_url: str, tickets_url: str):
    from rebar.opcert_service.config import OpcertServiceConfig

    return OpcertServiceConfig(review_remote_url=review_url, tickets_remote_url=tickets_url)


def test_opcert_workspace_arms_the_abort_on_its_fetches(tmp_path):
    from rebar.opcert_service import workspace as ws_mod

    seen: list[list[str]] = []

    def _fake_run(cmd, **kwargs):
        seen.append(list(cmd))
        # Fail the first fetch so _populate stops early with a WorkspaceError; the argv is
        # already captured, which is all this test is about.
        rc = 1 if "fetch" in cmd else 0
        return subprocess.CompletedProcess(cmd, rc, "", "boom")

    with mock.patch.object(ws_mod.subprocess, "run", _fake_run):
        with pytest.raises(ws_mod.WorkspaceError):
            ws_mod.prepare_workspace(
                _opcert_cfg("http://r.invalid/x.git", "http://t.invalid/x.git")
            )

    fetches = _fetch_argvs(seen)
    assert fetches, "no fetch was launched — fixture is wrong"
    for argv in fetches:
        _assert_armed(argv)
    # Local (non-network) git calls are NOT dressed up with transport options.
    local = [argv for argv in seen if "fetch" not in argv]
    assert local, seen
    assert all("http.lowSpeedTime=10" not in argv for argv in local), local


def test_opcert_workspace_stalled_remote_aborts_as_a_stall(tight_stall):
    from rebar.opcert_service import workspace as ws_mod

    port, stop = serve("stall", seconds=30)
    try:
        with pytest.raises(ws_mod.WorkspaceError) as excinfo:
            ws_mod.prepare_workspace(
                _opcert_cfg(f"http://127.0.0.1:{port}/x.git", f"http://127.0.0.1:{port}/y.git")
            )
    finally:
        stop.set()
    message = str(excinfo.value)
    assert git_fetch.is_stall_abort(message), message
    assert "timed out after" not in message, message


# ── site 3: `rebar init` fetching the ticket branch ──────────────────────────


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return str(root)


def test_init_fetch_arms_the_abort(repo, monkeypatch):
    from rebar._commands import init as init_mod

    seen: list[list[str]] = []

    def _fake_run_git(cwd, *args, **kwargs):
        seen.append(["git", *args])
        return subprocess.CompletedProcess(["git", *args], 0, "", "")

    monkeypatch.setattr(init_mod, "run_git", _fake_run_git)
    init_mod._git_fetch(repo, "fetch", "origin", "tickets")

    assert seen, "no git child was launched"
    _assert_armed(seen[0])


def test_init_fetch_stalled_remote_aborts_as_a_stall(repo, tight_stall):
    from rebar._commands import init as init_mod

    port, stop = serve("stall", seconds=30)
    try:
        proc = init_mod._git_fetch(repo, "fetch", f"http://127.0.0.1:{port}/x.git", "tickets")
    finally:
        stop.set()
    assert proc.returncode != 0
    assert git_fetch.is_stall_abort(proc.stderr), proc.stderr
    assert "timed out after" not in proc.stderr, proc.stderr
