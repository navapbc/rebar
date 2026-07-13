"""Happy-path spec for the out-of-band trusted-environment config + required-environment check
(story 42d1 / urban-dihydric-trogon).

The ONLY tests the implementation subagent sees. Pins the approved-design happy path:
* the loader reads `.rebar/trusted_environments.yaml` and exposes an environment's pinned keyring;
* `verify_required_environment` certifies an op-cert that is genuinely signed by the pinned key of
  the required environment.

The adversarial matrix — absent file (fail-open), malformed file (located error), an environment
not in the pinned set, and the keyid-spoof case (verify against the PINNED key, never the cert's
self-claimed keyid) — lives in the held-out companion `test_trusted_env_heldout.py`.

Real ssh-keygen + real temp git repo (the era ancestry rule runs against real commits).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from rebar.attest import opcert, sshsig, trusted_env

try:
    sshsig.ensure_available()
    _SSH_OK = True
except Exception:  # noqa: BLE001
    _SSH_OK = False

pytestmark = pytest.mark.skipif(not _SSH_OK, reason="ssh-keygen >= 8.9 required for SSHSIG")

ENV_ID = "trusted-ci@rebar.test"
TICKET = "abcd-1234-ef01-5678"
MATERIAL = "0123456789abcdef"
KIND = "completion-verifier"


def _keypair(tmp_path: Path, name: str) -> tuple[str, str]:
    key = tmp_path / name
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(key), "-N", "", "-q", "-C", name],
        check=True,
        capture_output=True,
    )
    parts = (tmp_path / f"{name}.pub").read_text().strip().split()
    return str(key), f"{parts[0]} {parts[1]}"


def _repo(tmp_path: Path) -> tuple[Path, str]:
    """A real git repo with one commit; return (repo_dir, commit_sha)."""
    repo = tmp_path / "code"
    repo.mkdir()
    for args in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "d@e.test"),
        ("git", "config", "user.name", "D"),
        ("git", "commit", "-q", "--allow-empty", "-m", "c0"),
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    return repo, sha


def _write_config(repo: Path, env_id: str, pub: str, added_at: str) -> None:
    rebar_dir = repo / ".rebar"
    rebar_dir.mkdir(exist_ok=True)
    (rebar_dir / "trusted_environments.yaml").write_text(
        "environments:\n"
        f"  - env_id: {env_id}\n"
        "    keys:\n"
        f"      - public_key: {pub}\n"
        f"        added_at_commit: {added_at}\n"
        "        revoked_at_commit: null\n",
        encoding="utf-8",
    )


def test_loader_reads_pinned_keyring(tmp_path: Path) -> None:
    """The loader reads `.rebar/trusted_environments.yaml` and exposes the environment's keyring."""
    repo, commit = _repo(tmp_path)
    _priv, pub = _keypair(tmp_path, "env")
    _write_config(repo, ENV_ID, pub, commit)

    keyring = trusted_env.trusted_env_keyring(ENV_ID, repo_root=str(repo))
    assert keyring is not None
    assert len(keyring) == 1
    assert keyring[0]["public_key"] == pub
    assert keyring[0]["added_at_commit"] == commit
    assert keyring[0]["revoked_at_commit"] is None


def test_verify_required_environment_certifies_pinned_signer(tmp_path: Path) -> None:
    """An op-cert signed by the required environment's pinned key certifies."""
    repo, commit = _repo(tmp_path)
    priv, pub = _keypair(tmp_path, "env")
    _write_config(repo, ENV_ID, pub, commit)

    envelope = opcert.sign_opcert(
        TICKET, MATERIAL, commit, key_path=priv, kind=KIND, principal=ENV_ID
    )
    verdict = trusted_env.verify_required_environment(
        envelope, TICKET, MATERIAL, commit, ENV_ID, kind=KIND, repo_root=str(repo)
    )
    assert verdict.verified is True
    assert verdict.verdict == "certified"
