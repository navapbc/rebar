"""Happy-path oracle for TS3 (117b, epic gnu-whale-ichor transition): the merge-gate +
ledger reworked to the approved in-toto + commit-ancestry design.

The ONLY TS3 test the implementation sees. Pins the end-to-end happy path: with a current
identity holding a valid key and `identity.signing_key` configured, an event written
through the write seam (in-toto Statement `author_sig`) is classified `verified` by
`rebar verify-authorship`. The ledger schema, `key_not_valid_at_era`, forged
`bad-signature`, the untouched verify-signature schema, and `create_placeholder` are held
out.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import rebar
from rebar.attest import sshsig

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


def test_live_signed_event_verifies(store: Path, tmp_path: Path, monkeypatch) -> None:
    """An event written through the seam by an identity with a valid key (in-toto
    Statement author_sig) is classified `verified` by the merge-gate."""
    priv, pub = _keypair(tmp_path, "author")
    ident = rebar.create_identity("Ada", "ada@example.com", keys=[pub], repo_root=str(store))
    rebar.use_identity(ident, repo_root=str(store))
    monkeypatch.setenv("REBAR_IDENTITY_SIGNING_KEY", priv)

    # Written through the seam → author_sig is an in-toto Statement envelope.
    tid = rebar.create_ticket("task", "signed work", repo_root=str(store))
    rebar.comment(tid, "a signed comment", repo_root=str(store))

    env = {
        **os.environ,
        "REBAR_ROOT": str(store),
        "REBAR_IDENTITY_SIGNING_KEY": priv,
        "REBAR_IDENTITY_REQUIRE_AUTHENTICATED": "1",
    }
    res = subprocess.run(
        ["rebar", "verify-authorship", "--all"],
        cwd=store,
        env=env,
        capture_output=True,
        text=True,
    )
    # require_authenticated ON + every in-scope event verified ⇒ exit 0, ≥1 verified.
    assert res.returncode == 0, res.stdout + res.stderr
    out = (res.stdout + res.stderr).lower()
    assert "verified" in out
    assert "not verified" not in out
