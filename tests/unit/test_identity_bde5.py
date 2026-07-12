"""Happy-path oracle for bde5 (epic gnu-whale-ichor): the authorship attest kind.

The ONLY bde5 test the implementation sees. Pins the public contract on the happy
path: the `rebar.authorship.v1` policy is registered at import, and an event payload
signed with an identity's private key verifies against that identity's in-band
public keys. Cross-identity / tamper / unknown-identity rejection is held out.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
from rebar.attest import authorship, dsse, registry, sshsig

# Skip the whole module if ssh-keygen can't do SSHSIG on this host.
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
    """Generate an ed25519 keypair; return (private_key_path, public_key_line)."""
    key = tmp_path / name
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(key), "-N", "", "-q", "-C", name],
        check=True,
        capture_output=True,
    )
    pub = (tmp_path / f"{name}.pub").read_text().strip()
    # authorized-keys line = "<keytype> <keydata> [comment]"; keep type+data.
    parts = pub.split()
    return str(key), f"{parts[0]} {parts[1]}"


def test_authorship_policy_registered_at_import() -> None:
    """The rebar.authorship.v1 policy is live from `import rebar.attest` alone —
    no explicit register call needed."""
    pol = registry.resolve("rebar.authorship.v1")
    assert pol is not None
    assert pol.scheme == "sshsig"
    assert pol.namespace == "rebar.authorship.v1"


def test_sign_then_verify_roundtrip(store: Path, tmp_path: Path) -> None:
    """An event payload signed with A's key verifies against A's in-band keys."""
    priv, pub = _keypair(tmp_path, "alice")
    a = rebar.create_identity("Alice", "alice@example.com", keys=[pub], repo_root=str(store))

    payload = b'{"uuid":"e1","author_id":"' + a.encode() + b'","data":{"x":1}}'
    envelope = authorship.sign_authorship(payload, priv, principal=a)
    assert isinstance(envelope, dsse.Envelope)

    verdict = authorship.verify_authorship(envelope, a, repo_root=str(store))
    assert verdict.verified is True
