"""Visible trusted-environment happy path (42d1, Option B 4214).

Load the pinned keyring from .rebar/trusted_environments.yaml and verify a
cert genuinely signed by the required environment's key at its tickets-branch
storage-anchor era. ssh-keygen and store positions exercise the path.
test_trusted_env_heldout covers missing/malformed config, absent environment,
and spoofed keyid."""

from __future__ import annotations

from pathlib import Path

import pytest
from _opcert_helpers import keypair, store_with_chain

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
# merged_log_commit is a signed SUBJECT field only under Option B (no key-validity semantics).
MERGED = "0" * 40


def _write_config(repo: Path, env_id: str, pub: str, added_at_position: str) -> None:
    rebar_dir = repo / ".rebar"
    rebar_dir.mkdir(exist_ok=True)
    (rebar_dir / "trusted_environments.yaml").write_text(
        "environments:\n"
        f"  - env_id: {env_id}\n"
        "    keys:\n"
        f"      - public_key: {pub}\n"
        f"        added_at_log_position: {added_at_position}\n"
        "        revoked_at_log_position: null\n",
        encoding="utf-8",
    )


def test_loader_reads_pinned_keyring(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The loader reads `.rebar/trusted_environments.yaml` and exposes the environment's keyring."""
    repo, _tracker, pos = store_with_chain(tmp_path, monkeypatch, 3)
    _priv, pub = keypair(tmp_path, "env")
    _write_config(repo, ENV_ID, pub, pos[0][0])

    keyring = trusted_env.trusted_env_keyring(ENV_ID, repo_root=str(repo))
    assert keyring is not None
    assert len(keyring) == 1
    assert keyring[0]["public_key"] == pub
    assert keyring[0]["added_at_log_position"] == pos[0][0]
    assert keyring[0]["revoked_at_log_position"] is None


def test_verify_required_environment_certifies_pinned_signer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An op-cert signed by the required environment's pinned key certifies when the key is valid at
    the cert's storage anchor."""
    repo, _tracker, pos = store_with_chain(tmp_path, monkeypatch, 3)
    priv, pub = keypair(tmp_path, "env")
    _write_config(repo, ENV_ID, pub, pos[0][0])

    envelope = opcert.sign_opcert(
        TICKET, MATERIAL, MERGED, key_path=priv, kind=KIND, principal=ENV_ID
    )
    verdict = trusted_env.verify_required_environment(
        envelope,
        TICKET,
        MATERIAL,
        MERGED,
        ENV_ID,
        kind=KIND,
        storage_anchor_commit=pos[-1][1],
        storage_anchor_position=pos[-1][0],
        repo_root=str(repo),
    )
    assert verdict.verified is True
    assert verdict.verdict == "certified"
