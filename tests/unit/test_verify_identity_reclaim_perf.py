"""Enforced ``verify-identity`` stays bounded on reclaimed and ordinary history (bug 84f6).

A reclaim replaces the tickets history with a checkpoint commit that adds compacted events
directly as ``*.json.retired`` files, so no ``*.json`` add exists for them. The position map
must still resolve those positions, or every ledger entry and every keyring lookup walks the
whole history once. Era verification must also answer key ancestry from one in-memory graph
instead of one ``git merge-base`` subprocess per keyring record per event, and the SSHSIG
verifier must not re-probe the OpenSSH version on every signature.

These tests assert call shape, never wall-clock time.
"""

from __future__ import annotations

import itertools
import json
import subprocess
from pathlib import Path

import pytest

import rebar
from rebar._commands import compact as _compact
from rebar._commands import verify_authorship as V
from rebar._commands._seam import tracker_dir
from rebar.attest import authorship, sshsig

try:
    sshsig.ensure_available()
    _SSH_OK = True
except Exception:  # noqa: BLE001 — best-effort SSHSIG availability probe; skip if unavailable
    _SSH_OK = False

_needs_ssh = pytest.mark.skipif(not _SSH_OK, reason="ssh-keygen >= 8.9 required for SSHSIG")


def _git(cwd: Path | str, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _keypair(tmp_path: Path, name: str) -> tuple[str, str]:
    key = tmp_path / name
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(key), "-N", "", "-q", "-C", name],
        check=True,
        capture_output=True,
    )
    parts = (tmp_path / f"{name}.pub").read_text(encoding="utf-8").strip().split()
    return str(key), f"{parts[0]} {parts[1]}"


@pytest.fixture
def reclaimed_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str]:
    """A signed store whose work ticket is compacted, then whose tracker history is replaced
    by ONE root commit of its current tree, the shape a reclaim checkpoint leaves behind.

    Returns ``(repo, tracker)``."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("init", "-q"),
        ("config", "user.email", "dev@example.com"),
        ("config", "user.name", "Dev"),
        ("commit", "-q", "--allow-empty", "-m", "init"),
    ):
        _git(repo, *args)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    monkeypatch.delenv("REBAR_IDENTITY_ENFORCE_SINCE", raising=False)
    rebar.init_repo(repo_root=str(repo))
    priv, pub = _keypair(tmp_path, "ada-key")
    ident = rebar.create_identity("Ada", "ada@example.com", keys=[pub], repo_root=str(repo))
    rebar.use_identity(ident, repo_root=str(repo))
    monkeypatch.setenv("REBAR_IDENTITY_SIGNING_KEY", priv)
    tid = rebar.create_ticket("task", "signed work", repo_root=str(repo))
    for n in range(3):
        rebar.comment(tid, f"signed comment {n}", repo_root=str(repo))
    rc = _compact.compact_cli(
        [tid, "--threshold=0", "--horizon=0", "--skip-sync"], repo_root=str(repo)
    )
    assert rc == 0
    tracker = str(tracker_dir(str(repo)))
    assert list(Path(tracker, tid).glob("*.json.retired")), "compaction retired no events"
    root = _git(tracker, "commit-tree", "HEAD^{tree}", "-m", "reclaim checkpoint")
    _git(tracker, "update-ref", "HEAD", root)
    return repo, tracker


def _ledger_positions(tracker: str) -> list[str]:
    positions = []
    for snap in Path(tracker).glob("*/*-SNAPSHOT.json"):
        event = json.loads(snap.read_text(encoding="utf-8"))
        for entry in event["data"]["compiled_state"].get("authorship_ledger") or []:
            positions.append(entry["position"]["position"])
    return positions


@_needs_ssh
def test_position_map_resolves_positions_added_only_as_retired_files(
    reclaimed_store: tuple[Path, str],
) -> None:
    repo, tracker = reclaimed_store
    root = _git(tracker, "rev-parse", "HEAD")
    positions = _ledger_positions(tracker)
    assert positions, "the compacted ticket carries no ledger entries"

    position_map = authorship.build_position_commit_map(repo_root=str(repo))

    assert {p: position_map.get(p) for p in positions} == dict.fromkeys(positions, root)


@_needs_ssh
def test_enforced_scan_of_a_reclaimed_store_needs_no_per_position_walk(
    reclaimed_store: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _tracker = reclaimed_store
    monkeypatch.setenv("REBAR_IDENTITY_REQUIRE_AUTHENTICATED", "1")
    walks = [0]
    orig = authorship.resolve_position_commit

    def counting(*a, **k):
        walks[0] += 1
        return orig(*a, **k)

    monkeypatch.setattr(authorship, "resolve_position_commit", counting)

    rc = V.cli(["--all", "--root", str(repo)])

    assert rc == 0, "every signed event on the reclaimed history must verify"
    assert walks[0] == 0, "a map miss walked the whole history per position"


@_needs_ssh
def test_enforced_scan_runs_no_merge_base_subprocess_for_era_checks(
    reclaimed_store: tuple[Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _tracker = reclaimed_store
    monkeypatch.setenv("REBAR_IDENTITY_REQUIRE_AUTHENTICATED", "1")
    merge_bases = [0]
    real_run = subprocess.run

    def counting(argv, *a, **k):
        if isinstance(argv, list) and "merge-base" in argv:
            merge_bases[0] += 1
        return real_run(argv, *a, **k)

    monkeypatch.setattr(authorship.subprocess, "run", counting)

    rc = V.cli(["--all", "--root", str(repo)])

    assert rc == 0
    assert merge_bases[0] == 0, "era verification shelled out to git merge-base"


def _commit(repo: Path, name: str) -> str:
    (repo / name).write_text(name, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", name)
    return _git(repo, "rev-parse", "HEAD")


def _merge_base_says(repo: Path, ancestor: str, descendant: str) -> bool:
    proc = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", ancestor, descendant],
        capture_output=True,
        check=False,
    )
    return proc.returncode == 0


def test_ancestry_oracle_agrees_with_merge_base(tmp_path: Path) -> None:
    repo = tmp_path / "graph"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "dev@example.com")
    _git(repo, "config", "user.name", "Dev")
    a = _commit(repo, "a")
    b = _commit(repo, "b")
    _git(repo, "checkout", "-q", "-b", "side", a)
    c = _commit(repo, "c")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge", "side")
    m = _git(repo, "rev-parse", "HEAD")
    d = _commit(repo, "d")
    # A commit outside HEAD's history: the oracle must defer to git for it.
    _git(repo, "checkout", "-q", "-b", "stray", c)
    stray = _commit(repo, "stray")
    _git(repo, "checkout", "-q", "main")

    oracle = authorship.build_ancestry_oracle(str(repo))
    assert oracle is not None

    commits = [a, b, c, m, d, stray]
    for x, y in itertools.product(commits, repeat=2):
        assert oracle(x, y) is _merge_base_says(repo, x, y), (x, y)
    assert oracle("0" * 40, d) is False
    assert oracle(d, "0" * 40) is False


def test_openssh_version_is_probed_once_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    probes = [0]

    def fake_run(argv, *a, **k):
        probes[0] += 1
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="OpenSSH_9.5p1")

    sshsig.ssh_keygen_version.cache_clear()
    monkeypatch.setattr(sshsig.subprocess, "run", fake_run)
    try:
        versions = {sshsig.ssh_keygen_version() for _ in range(5)}
    finally:
        sshsig.ssh_keygen_version.cache_clear()

    assert versions == {(9, 5)}
    assert probes[0] == 1
