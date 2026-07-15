"""WS3: fsck ⇄ ensure-registry wiring + MCP-startup sweep.

Covers the surfaces WS3 owns: the read-only ``ensures: N/M applied`` line (derived
without sweeping, text-only / not in ``--output json``), the fold of ``run_ensures``
into ``fsck --repair`` (converge + fsck-level idempotency), the ``no_mutate``
read-only guarantee, and the best-effort MCP-startup sweep. The authoritative
git-log zero-commit / hot-path / concurrency suite lives in WS5.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import rebar
from rebar._commands import fsck as fsck_mod
from rebar._store import ensures


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.email", "t@e"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=r, check=True)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "base"], cwd=r, check=True)
    monkeypatch.setenv("REBAR_ROOT", str(r))
    monkeypatch.delenv("REBAR_TRACKER_DIR", raising=False)
    monkeypatch.setenv("REBAR_SYNC_PULL", "off")
    monkeypatch.setenv("REBAR_SYNC_PUSH", "off")
    rebar.init_repo(repo_root=str(r))
    ensures._reset_pending_cache()
    return r


def _tracker(repo: Path) -> Path:
    return repo / ".tickets-tracker"


def _tickets_head(tracker: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(tracker), "rev-parse", "tickets"], capture_output=True, text=True
    ).stdout.strip()


# ── read-only ensures: N/M line ───────────────────────────────────────────────
def test_plain_fsck_prints_converged_line(repo: Path) -> None:
    out = rebar.fsck(repo_root=str(repo))
    assert "ensures: 6/6 applied" in out
    assert "run `rebar fsck --repair`" not in out  # converged → no nudge


def test_plain_fsck_pending_line_and_no_sweep(repo: Path) -> None:
    tracker = _tracker(repo)
    (tracker / ensures.APPLIED_MARKER).unlink()  # simulate a pre-feature/pending store
    before = _tickets_head(tracker)
    out = rebar.fsck(repo_root=str(repo))
    assert "ensures: 0/6 applied" in out
    assert "run `rebar fsck --repair` to converge" in out
    # read-only fsck must NOT sweep: no marker rewritten, no commits.
    assert not (tracker / ensures.APPLIED_MARKER).exists()
    assert _tickets_head(tracker) == before


def test_ensures_line_absent_from_json_output(repo: Path, capsys) -> None:
    """The informational lowercase `ensures:` line is text-only: it must not appear
    in --output json nor inflate issue_count."""
    tracker = _tracker(repo)
    (tracker / ensures.APPLIED_MARKER).unlink()
    fsck_mod.fsck_cli(["--output", "json"], repo_root=str(repo))
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["issue_count"] == 0  # a pending marker is not a structural issue
    assert "ensures" not in json.dumps(payload).lower()


# ── --repair folds run_ensures ────────────────────────────────────────────────
def test_repair_converges_pending_store(repo: Path, capsys) -> None:
    tracker = _tracker(repo)
    (tracker / ensures.APPLIED_MARKER).unlink()
    ensures._reset_pending_cache()
    fsck_mod.fsck_cli(["--repair"], repo_root=str(repo))
    out = capsys.readouterr().out
    assert "ensures: swept 6 unit(s)" in out
    # marker rewritten → converged, and the re-scan line now reads 6/6.
    assert ensures.applied_ids(tracker) == set(ensures.REGISTRY_IDS)
    assert "ensures: 6/6 applied" in out


def test_repair_ensure_phase_is_idempotent(repo: Path, capsys) -> None:
    """fsck-level idempotency: a second --repair on a converged store makes no new
    commits (the authoritative git-log assertion also lives in WS5)."""
    fsck_mod.fsck_cli(["--repair"], repo_root=str(repo))
    capsys.readouterr()
    before = _tickets_head(_tracker(repo))
    fsck_mod.fsck_cli(["--repair"], repo_root=str(repo))
    out = capsys.readouterr().out
    assert _tickets_head(_tracker(repo)) == before, "converged --repair must not commit"
    assert "0 changed" in out


def test_dry_run_repair_does_not_sweep(repo: Path, capsys) -> None:
    tracker = _tracker(repo)
    (tracker / ensures.APPLIED_MARKER).unlink()
    ensures._reset_pending_cache()
    fsck_mod.fsck_cli(["--repair", "--dry-run"], repo_root=str(repo))
    out = capsys.readouterr().out
    assert "swept" not in out  # dry-run must not run the sweep
    assert not (tracker / ensures.APPLIED_MARKER).exists()


def test_no_mutate_library_fsck_never_sweeps(repo: Path) -> None:
    tracker = _tracker(repo)
    (tracker / ensures.APPLIED_MARKER).unlink()
    before = _tickets_head(tracker)
    rebar.fsck(repo_root=str(repo))  # no_mutate read-only surface
    assert not (tracker / ensures.APPLIED_MARKER).exists()
    assert _tickets_head(tracker) == before


# ── MCP startup best-effort sweep ─────────────────────────────────────────────
class _StubServer:
    def __init__(self) -> None:
        self.ran = False

    def run(self) -> None:
        self.ran = True


def test_mcp_startup_sweeps_before_run(repo: Path, monkeypatch) -> None:
    from rebar import mcp_server

    order: list[str] = []
    stub = _StubServer()

    def _fake_run_ensures(tracker, **kw):
        order.append("ensures")
        return []

    def _fake_build_server():
        def _run():
            order.append("run")
            stub.ran = True

        stub.run = _run  # type: ignore[method-assign]
        return stub

    monkeypatch.setattr(ensures, "run_ensures", _fake_run_ensures)
    monkeypatch.setattr(mcp_server, "build_server", _fake_build_server)
    monkeypatch.setattr("sys.argv", ["rebar-mcp"])
    mcp_server.main()
    assert order == ["ensures", "run"], "run_ensures must precede build_server().run()"


def test_mcp_help_skips_sweep(repo: Path, monkeypatch, capsys) -> None:
    from rebar import mcp_server

    called = {"ensures": False}
    monkeypatch.setattr(
        ensures, "run_ensures", lambda *a, **k: called.__setitem__("ensures", True) or []
    )
    monkeypatch.setattr("sys.argv", ["rebar-mcp", "--help"])
    mcp_server.main()  # early return on --help
    assert called["ensures"] is False


def test_mcp_startup_never_aborts_on_sweep_error(repo: Path, monkeypatch) -> None:
    from rebar import mcp_server

    stub = _StubServer()

    def _boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(ensures, "run_ensures", _boom)
    monkeypatch.setattr(mcp_server, "build_server", lambda: stub)
    monkeypatch.setattr("sys.argv", ["rebar-mcp"])
    mcp_server.main()
    assert stub.ran, "boot must proceed to build_server().run() even if the sweep raises"
