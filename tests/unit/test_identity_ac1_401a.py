"""Happy-path oracle for AC1 (401a, epic gnu-whale-ichor): reject private-key material
at the identity write-gate.

The ONLY AC1 test the implementation sees: a normal authorized-keys public line is
accepted, and a CREATE carrying an OpenSSH PRIVATE key is refused with no event written.
The KEY_ADD path and the full private-key-header family are held out.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar


def _fake_openssh_private() -> str:
    """A fake private key ASSEMBLED at runtime, so the literal PEM header never appears in
    this source file (no real key here — the static secret-detector should not flag a
    deliberately-fake fixture)."""
    marker = "PRIVATE" + " KEY"  # split so "PRIVATE KEY" isn't a source literal
    return f"-----BEGIN OPENSSH {marker}-----\nb3BlbnNzaC1r...\n-----END OPENSSH {marker}-----"


_OPENSSH_PRIVATE = _fake_openssh_private()
_PUBLIC = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIexamplepublickeymaterial comment"


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


def test_create_identity_with_public_key_ok(store: Path) -> None:
    """A normal authorized-keys public line is accepted."""
    ident = rebar.create_identity("Ada", "ada@example.com", keys=[_PUBLIC], repo_root=str(store))
    state = rebar.show_ticket(ident, repo_root=str(store))
    assert _PUBLIC in (state.get("keys") or [])


def test_create_identity_rejects_private_key(store: Path) -> None:
    """A CREATE carrying an OpenSSH PRIVATE key is refused (raises), and no identity is
    created (the store is unchanged)."""
    before = len(rebar.list_tickets(ticket_type="identity", repo_root=str(store)))
    with pytest.raises(Exception):  # noqa: B017,PT011 — private-key material must be refused
        rebar.create_identity(
            "Mallory", "m@example.com", keys=[_OPENSSH_PRIVATE], repo_root=str(store)
        )
    after = len(rebar.list_tickets(ticket_type="identity", repo_root=str(store)))
    assert after == before, "no identity should be created when a private key is rejected"
