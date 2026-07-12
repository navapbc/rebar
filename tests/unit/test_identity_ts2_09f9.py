"""Happy-path oracle for TS2 (09f9, epic gnu-whale-ichor transition): git-commit-ancestry
key validity (replaces the HLC-epoch model).

The ONLY TS2 test the implementation sees. Pins the approved-design happy path: a genesis
key lands in the keyring as a POSITION-based record (no epoch fields), and an authorship
signature by that key verifies at the commit of an event written after the key was added.
The security/boundary/backdating/intra-commit/fail-closed cases are held out.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
from rebar._commands._seam import tracker_dir
from rebar.attest import authorship, sshsig

try:
    sshsig.ensure_available()
    _SSH_OK = True
except Exception:  # noqa: BLE001 — best-effort SSHSIG availability probe; skip if unavailable
    _SSH_OK = False

pytestmark = pytest.mark.skipif(not _SSH_OK, reason="ssh-keygen >= 8.9 required for SSHSIG")


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # Keep events unfolded so keyring events stay inspectable.
    monkeypatch.setenv("REBAR_COMPACTION_HORIZON_NS", "9" * 18)
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "dev@example.com"),
        ("git", "config", "user.name", "Dev"),
        ("git", "commit", "-q", "--allow-empty", "-m", "init"),
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    return repo


def _keypair(tmp_path: Path, name: str) -> tuple[str, str]:
    key = tmp_path / name
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(key), "-N", "", "-q", "-C", name],
        check=True,
        capture_output=True,
    )
    parts = (tmp_path / f"{name}.pub").read_text().strip().split()
    return str(key), f"{parts[0]} {parts[1]}"


def _tracker(store: Path) -> str:
    return str(tracker_dir(str(store)))


def _head(store: Path) -> str:
    """The current tickets-branch HEAD commit SHA (the commit the last write sealed)."""
    cp = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=_tracker(store),
        capture_output=True,
        text=True,
        check=True,
    )
    return cp.stdout.strip()


def test_genesis_key_records_position_not_epoch(store: Path, tmp_path: Path) -> None:
    """The first (genesis/TOFU) key on a keyless identity lands in the keyring as a
    position-based record: {public_key, added_at: <position>, revoked_at: None} with NO
    epoch fields."""
    _priv, pub = _keypair(tmp_path, "genesis")
    ident = rebar.create_identity("Ada", "ada@example.com", repo_root=str(store))
    rebar.add_identity_key(ident, pub, signature=None, repo_root=str(store))

    state = rebar.show_ticket(ident, repo_root=str(store))
    ring = state["keyring"]
    rec = next(r for r in ring if r["public_key"] == pub)
    assert rec["revoked_at"] is None
    assert isinstance(rec["added_at"], str) and rec["added_at"]  # a position prefix
    assert "added_epoch" not in rec and "revoked_epoch" not in rec
    assert "keyring_epoch" not in state


def test_verify_at_commit_after_add(store: Path, tmp_path: Path) -> None:
    """An authorship signature by a genesis key verifies at the commit of an event
    written AFTER the key was added (the event's commit descends the key's add-commit)."""
    priv, pub = _keypair(tmp_path, "genesis")
    ident = rebar.create_identity("Ada", "ada@example.com", repo_root=str(store))
    rebar.add_identity_key(ident, pub, signature=None, repo_root=str(store))

    # A later event on the tickets branch — its commit descends the KEY_ADD commit.
    later = rebar.create_ticket("task", "later work", repo_root=str(store))
    event_commit = _head(store)

    payload = b'{"uuid":"later-ev","data":{"x":1}}'
    envelope = authorship.sign_authorship(payload, priv, principal=ident)
    verdict = authorship.verify_authorship_at_commit(
        envelope, ident, event_commit, None, repo_root=str(store)
    )
    assert verdict.verified is True
    assert later  # sanity
