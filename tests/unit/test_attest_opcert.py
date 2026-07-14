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

from pathlib import Path

import pytest
from _opcert_helpers import keypair, store_with_chain

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
KIND = "completion-verifier"
# merged_log_commit is a signed SUBJECT field only under Option B (no key-validity semantics).
MERGED = "0" * 40


def test_opcert_kind_registered() -> None:
    """``rebar.opcert.v1`` is pinned in the registry POLICY table to the sshsig scheme."""
    from rebar.attest import registry

    policy = registry.resolve(opcert.OPCERT_KIND)
    assert policy is not None
    assert policy.scheme == "sshsig"
    assert policy.namespace == opcert.OPCERT_NAMESPACE == "rebar.opcert.v1"


def test_opcert_sign_and_verify_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A cert signed by environment E, bound to {ticket, material, merged-log commit}, verifies
    against E's pinned key when the subject matches and the key is valid at the STORAGE ANCHOR (a
    tickets-branch commit; the key's add-position resolves to an ancestor of it)."""
    repo, _tracker, pos = store_with_chain(tmp_path, monkeypatch, 3)
    priv, pub = keypair(tmp_path, "env")
    keyring = [
        {"public_key": pub, "added_at_log_position": pos[0][0], "revoked_at_log_position": None}
    ]

    envelope = opcert.sign_opcert(
        TICKET, MATERIAL, MERGED, key_path=priv, kind=KIND, principal=ENV_ID
    )
    verdict = opcert.verify_opcert(
        envelope,
        TICKET,
        MATERIAL,
        MERGED,
        keyring,
        kind=KIND,
        principal=ENV_ID,
        storage_anchor_commit=pos[-1][1],
        storage_anchor_position=pos[-1][0],
        repo_root=str(repo),
    )
    assert verdict.verified is True
    assert verdict.verdict == "certified"
