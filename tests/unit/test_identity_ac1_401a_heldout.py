"""HELD-OUT oracle for AC1 (401a) — the implementation MUST NOT see this file.

The guard the happy path can't cover: the KEY_ADD path is guarded too, the full
private-key header family is rejected (case-insensitively), and a rejected KEY_ADD
appends no event.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
from rebar._commands._seam import tracker_dir

_PUBLIC = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIexamplepublickeymaterial comment"


def _fake_priv(kind: str = "") -> str:
    """A fake private-key PEM header for testing the guard, ASSEMBLED at runtime so the
    literal header never appears in this source file (there is no real key here — the body
    is a placeholder — and assembling it keeps the static secret-detector from flagging a
    deliberately-fake fixture)."""
    marker = "PRIVATE" + " KEY"  # split so the literal "PRIVATE KEY" isn't in source text
    k = f"{kind} " if kind else ""
    return f"-----BEGIN {k}{marker}-----\nx\n-----END {k}{marker}-----"


_PRIVATE_HEADERS = [
    _fake_priv("OPENSSH"),
    _fake_priv("RSA"),
    _fake_priv("EC"),
    _fake_priv("DSA"),
    _fake_priv(),
    _fake_priv("ENCRYPTED"),
    _fake_priv("OPENSSH").lower(),  # case-insensitive
]


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


@pytest.mark.parametrize("private_key", _PRIVATE_HEADERS)
def test_create_rejects_all_private_key_headers(store: Path, private_key: str) -> None:
    """Every private-key header (RSA/EC/DSA/OPENSSH/PKCS8/ENCRYPTED, case-insensitive) is
    refused on the CREATE path."""
    with pytest.raises(Exception):  # noqa: B017,PT011 — must reject private-key material
        rebar.create_identity("M", "m@example.com", keys=[private_key], repo_root=str(store))


def test_add_identity_key_rejects_private_key(store: Path) -> None:
    """The KEY_ADD path is guarded too: adding a private key raises and appends no event."""
    ident = rebar.create_identity("Ada", "ada@example.com", repo_root=str(store))
    tdir = Path(tracker_dir(str(store))) / ident
    before = len(list(tdir.glob("*.json")))
    with pytest.raises(Exception):  # noqa: B017,PT011 — private-key material must be refused
        rebar.add_identity_key(ident, _fake_priv("RSA"), repo_root=str(store))
    after = len(list(tdir.glob("*.json")))
    assert after == before, "a rejected KEY_ADD must append no event"


def test_add_identity_key_accepts_public(store: Path) -> None:
    """A normal public key is still accepted on KEY_ADD (genesis)."""
    ident = rebar.create_identity("Ada", "ada@example.com", repo_root=str(store))
    rebar.add_identity_key(ident, _PUBLIC, repo_root=str(store))
    state = rebar.show_ticket(ident, repo_root=str(store))
    assert _PUBLIC in (state.get("keys") or [])
