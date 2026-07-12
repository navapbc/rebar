"""Happy-path oracle for e165 (epic gnu-whale-ichor): TOFU genesis + signed
key rotation + epoch-scoped validity.

The ONLY e165 test the implementation sees. Pins the happy path: a genesis key is
added trust-on-first-use (no signature), it lands in the identity's keyring at epoch
0, and an authorship signature made by that key verifies at its valid epoch. The
security cases (unsigned rotation refused, revoked-epoch rejection, snapshot safety,
epoch-not-timestamp) are held out.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
from rebar.attest import authorship, sshsig

try:
    sshsig.ensure_available()
    _SSH_OK = True
except Exception:  # noqa: BLE001 — best-effort SSHSIG availability probe; skip if unavailable
    _SSH_OK = False

pytestmark = pytest.mark.skipif(not _SSH_OK, reason="ssh-keygen >= 8.9 required for SSHSIG")


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
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


def test_genesis_key_add_tofu(store: Path, tmp_path: Path) -> None:
    """The FIRST key on a keyless identity is added trust-on-first-use (no signature),
    landing in the keyring at epoch 0."""
    _priv, pub = _keypair(tmp_path, "genesis")
    ident = rebar.create_identity("Ada", "ada@example.com", repo_root=str(store))

    rebar.add_identity_key(ident, pub, signature=None, repo_root=str(store))

    state = rebar.show_ticket(ident, repo_root=str(store))
    ring = state["keyring"]
    assert any(
        r["public_key"] == pub and r["added_epoch"] == 0 and r["revoked_epoch"] is None
        for r in ring
    )


def test_verify_at_valid_epoch(store: Path, tmp_path: Path) -> None:
    """An authorship signature by a genesis key verifies at its valid epoch."""
    priv, pub = _keypair(tmp_path, "genesis")
    ident = rebar.create_identity("Ada", "ada@example.com", repo_root=str(store))
    rebar.add_identity_key(ident, pub, signature=None, repo_root=str(store))

    payload = b'{"uuid":"e1","data":{"x":1}}'
    envelope = authorship.sign_authorship(payload, priv, principal=ident)
    verdict = authorship.verify_authorship_at_epoch(envelope, ident, 0, repo_root=str(store))
    assert verdict.verified is True
