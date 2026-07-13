"""Happy-path spec for the op-cert kind (story 368c / garlicky-deviant-kakapo).

The ONLY op-cert tests the implementation subagent sees. Pins the approved-design happy path:
an asymmetric environment-signed operation certificate (``rebar.opcert.v1``) round-trips —
signed by an environment's Ed25519 key, verified against that environment's pinned public key,
with the in-toto subject binding {ticket, material, merged-log commit} matching.

The adversarial matrix — cross-key rejection, replay onto a different ticket / mutated material,
explicit-SHA era rotation (key_not_valid_at_era vs mismatch), and ssh-keygen-absent
fail-closed — lives in the held-out companion ``test_attest_opcert_heldout.py`` (NOT given to the
implementer). Real integration against ``ssh-keygen`` (no new dependency); skip only when
OpenSSH >= 8.9 is unavailable.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from rebar.attest import opcert, sshsig

try:
    sshsig.ensure_available()
    _SSH_OK = True
except Exception:  # noqa: BLE001 — availability probe; skip if ssh-keygen missing/old
    _SSH_OK = False

pytestmark = pytest.mark.skipif(not _SSH_OK, reason="ssh-keygen >= 8.9 required for SSHSIG")

ENV_ID = "trusted-ci@rebar.test"
TICKET = "abcd-1234-ef01-5678"
MATERIAL = "0123456789abcdef"


def _keypair(tmp_path: Path, name: str) -> tuple[str, str]:
    """Return (private_key_path, 'ssh-ed25519 AAAA…' public line) for a fresh Ed25519 key."""
    key = tmp_path / name
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(key), "-N", "", "-q", "-C", name],
        check=True,
        capture_output=True,
    )
    parts = (tmp_path / f"{name}.pub").read_text().strip().split()
    return str(key), f"{parts[0]} {parts[1]}"


def _git_repo_with_commit(tmp_path: Path) -> tuple[Path, str]:
    """A real git repo with one commit; return (repo_dir, commit_sha) — the 'main' era anchor."""
    repo = tmp_path / "code"
    repo.mkdir()
    for args in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "d@e.test"),
        ("git", "config", "user.name", "D"),
        ("git", "commit", "-q", "--allow-empty", "-m", "c1"),
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    return repo, sha


def test_opcert_kind_registered() -> None:
    """``rebar.opcert.v1`` is pinned in the registry POLICY table to the sshsig scheme."""
    from rebar.attest import registry

    policy = registry.resolve(opcert.OPCERT_KIND)
    assert policy is not None
    assert policy.scheme == "sshsig"
    assert policy.namespace == opcert.OPCERT_NAMESPACE == "rebar.opcert.v1"


def test_opcert_sign_and_verify_roundtrip(tmp_path: Path) -> None:
    """A cert signed by environment E, bound to {ticket, material, commit}, verifies against E's
    pinned key when the subject matches and the key is valid at the bound commit."""
    repo, commit = _git_repo_with_commit(tmp_path)
    priv, pub = _keypair(tmp_path, "env")
    keyring = [{"public_key": pub, "added_at_commit": commit, "revoked_at_commit": None}]

    envelope = opcert.sign_opcert(TICKET, MATERIAL, commit, key_path=priv, principal=ENV_ID)
    verdict = opcert.verify_opcert(
        envelope, TICKET, MATERIAL, commit, keyring, principal=ENV_ID, repo_root=str(repo)
    )
    assert verdict.verified is True
    assert verdict.verdict == "certified"
