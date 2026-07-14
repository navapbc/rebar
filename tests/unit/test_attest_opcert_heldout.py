"""Held-out adversarial oracle for the op-cert kind (story 368c / garlicky-deviant-kakapo),
re-expressed for Option B (story 4214).

NOT shown to the implementation subagent. These are the cases that separate a real op-cert
implementation from one that fakes the happy path:

* cross-key rejection — a cert verifies ONLY against the signing environment's key;
* replay defense — the {ticket, material, merged-log commit} subject binding rejects a cert
  replayed onto a different ticket or onto a mutated material fingerprint;
* era rotation at the STORAGE ANCHOR — a key valid at the cert's storage anchor verifies; one past
  its ``revoked_at_log_position`` (relative to the anchor) surfaces the shared
  ``key_not_valid_at_era`` verdict (two-phase check); a genuinely foreign key surfaces
  ``mismatch``, not ``key_not_valid_at_era``;
* fail-closed — ssh-keygen absent never silently passes.

Under Option B the era boundaries are TICKETS-BRANCH log positions and validity is judged at the
certificate's storage anchor (a tickets-branch commit), so these tests run against a real rebar
store's position chain. Real ``ssh-keygen`` integration.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from _opcert_helpers import keypair, store_with_chain

from rebar.attest import opcert, registry, sshsig

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
# merged_log_commit is a signed SUBJECT field only (no key-validity semantics under Option B),
# so any stable string works as long as signer and verifier agree.
MERGED = "0" * 40


def _verify(env, repo, anchor, *, ticket=TICKET, material=MATERIAL, keyring, kind=KIND):
    return opcert.verify_opcert(
        env,
        ticket,
        material,
        MERGED,
        keyring,
        kind=kind,
        principal=ENV_ID,
        storage_anchor_commit=anchor[1],
        storage_anchor_position=anchor[0],
        repo_root=str(repo),
    )


# ---- cross-key rejection (AC1) ----------------------------------------------------------------


def test_verify_fails_against_different_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _tracker, pos = store_with_chain(tmp_path, monkeypatch, 3)
    priv, _pub = keypair(tmp_path, "env")
    _other_priv, other_pub = keypair(tmp_path, "other")
    # keyring pins a DIFFERENT environment key than the one that signed.
    keyring = [
        {
            "public_key": other_pub,
            "added_at_log_position": pos[0][0],
            "revoked_at_log_position": None,
        }
    ]
    env = opcert.sign_opcert(TICKET, MATERIAL, MERGED, key_path=priv, kind=KIND, principal=ENV_ID)
    verdict = _verify(env, repo, pos[-1], keyring=keyring)
    assert verdict.verified is False
    assert verdict.verdict == "mismatch"


# ---- replay defense via subject binding (AC2) -------------------------------------------------


def test_replay_onto_different_ticket_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _tracker, pos = store_with_chain(tmp_path, monkeypatch, 3)
    priv, pub = keypair(tmp_path, "env")
    keyring = [
        {"public_key": pub, "added_at_log_position": pos[0][0], "revoked_at_log_position": None}
    ]
    env = opcert.sign_opcert(TICKET, MATERIAL, MERGED, key_path=priv, kind=KIND, principal=ENV_ID)
    # Same, validly-signed envelope, presented as if it certified a DIFFERENT ticket.
    verdict = _verify(env, repo, pos[-1], ticket="9999-0000-0000-0000", keyring=keyring)
    assert verdict.verified is False
    assert verdict.verdict in {"subject_mismatch", "mismatch"}


def test_replay_onto_mutated_material_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _tracker, pos = store_with_chain(tmp_path, monkeypatch, 3)
    priv, pub = keypair(tmp_path, "env")
    keyring = [
        {"public_key": pub, "added_at_log_position": pos[0][0], "revoked_at_log_position": None}
    ]
    env = opcert.sign_opcert(TICKET, MATERIAL, MERGED, key_path=priv, kind=KIND, principal=ENV_ID)
    # The ticket's material changed after signing → the cert must no longer verify for it.
    verdict = _verify(env, repo, pos[-1], material="ffffffffffffffff", keyring=keyring)
    assert verdict.verified is False
    assert verdict.verdict in {"subject_mismatch", "mismatch"}


# ---- era rotation at the storage anchor + two-phase verdict (AC3) -----------------------------


def test_key_valid_at_era_verifies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Key added at an early log position, not revoked; storage anchor is a LATER tickets-branch
    commit → the add-position resolves to an ancestor of the anchor, so the key is valid there."""
    repo, _tracker, pos = store_with_chain(tmp_path, monkeypatch, 3)
    priv, pub = keypair(tmp_path, "env")
    keyring = [
        {"public_key": pub, "added_at_log_position": pos[0][0], "revoked_at_log_position": None}
    ]
    env = opcert.sign_opcert(TICKET, MATERIAL, MERGED, key_path=priv, kind=KIND, principal=ENV_ID)
    verdict = _verify(env, repo, pos[-1], keyring=keyring)
    assert verdict.verified is True
    assert verdict.verdict == "certified"


def test_key_past_rotation_surfaces_key_not_valid_at_era(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Key added early and revoked at a middle position; storage anchor is a LATER commit (the
    revoke-position resolves to an ancestor of the anchor) → the ONLY matching key is past its
    rotation. The signature is genuinely by that (historical) key, so the two-phase check must
    surface key_not_valid_at_era, NOT mismatch."""
    repo, _tracker, pos = store_with_chain(tmp_path, monkeypatch, 3)
    priv, pub = keypair(tmp_path, "env")
    keyring = [
        {
            "public_key": pub,
            "added_at_log_position": pos[0][0],
            "revoked_at_log_position": pos[1][0],
        }
    ]
    env = opcert.sign_opcert(TICKET, MATERIAL, MERGED, key_path=priv, kind=KIND, principal=ENV_ID)
    verdict = _verify(env, repo, pos[-1], keyring=keyring)
    assert verdict.verified is False
    assert verdict.verdict == "key_not_valid_at_era"


def test_key_not_yet_added_surfaces_key_not_valid_at_era(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Key added at a LATE log position, but the storage anchor is an EARLIER commit (the
    add-position is NOT an ancestor of the anchor) → key not yet valid there; signature is real →
    key_not_valid_at_era."""
    repo, _tracker, pos = store_with_chain(tmp_path, monkeypatch, 3)
    priv, pub = keypair(tmp_path, "env")
    keyring = [
        {"public_key": pub, "added_at_log_position": pos[-1][0], "revoked_at_log_position": None}
    ]
    env = opcert.sign_opcert(TICKET, MATERIAL, MERGED, key_path=priv, kind=KIND, principal=ENV_ID)
    verdict = _verify(env, repo, pos[0], keyring=keyring)
    assert verdict.verified is False
    assert verdict.verdict == "key_not_valid_at_era"


def test_foreign_key_surfaces_mismatch_not_era(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cert signed by a key that is NOT in the keyring at all (nor any era) → mismatch,
    never key_not_valid_at_era (the two-phase any-historical-key pass must fail first)."""
    repo, _tracker, pos = store_with_chain(tmp_path, monkeypatch, 3)
    foreign_priv, _ = keypair(tmp_path, "foreign")
    _, pinned_pub = keypair(tmp_path, "pinned")
    keyring = [
        {
            "public_key": pinned_pub,
            "added_at_log_position": pos[0][0],
            "revoked_at_log_position": None,
        }
    ]
    env = opcert.sign_opcert(
        TICKET, MATERIAL, MERGED, key_path=foreign_priv, kind=KIND, principal=ENV_ID
    )
    verdict = _verify(env, repo, pos[-1], keyring=keyring)
    assert verdict.verified is False
    assert verdict.verdict == "mismatch"


def test_empty_keyring_does_not_verify(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, _tracker, pos = store_with_chain(tmp_path, monkeypatch, 3)
    priv, _pub = keypair(tmp_path, "env")
    env = opcert.sign_opcert(TICKET, MATERIAL, MERGED, key_path=priv, kind=KIND, principal=ENV_ID)
    verdict = _verify(env, repo, pos[-1], keyring=[])
    assert verdict.verified is False


# ---- fail-closed (never a silent pass) --------------------------------------------------------


def test_ssh_keygen_absent_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If the verifier tool is unavailable, verify must fail closed — never a silent pass."""
    repo, _tracker, pos = store_with_chain(tmp_path, monkeypatch, 3)
    priv, pub = keypair(tmp_path, "env")
    keyring = [
        {"public_key": pub, "added_at_log_position": pos[0][0], "revoked_at_log_position": None}
    ]
    env = opcert.sign_opcert(TICKET, MATERIAL, MERGED, key_path=priv, kind=KIND, principal=ENV_ID)
    monkeypatch.setattr(sshsig, "ssh_keygen_version", lambda: None)
    verdict = _verify(env, repo, pos[-1], keyring=keyring)
    assert verdict.verified is False


def test_verdict_type_is_registry_verdict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """verify_opcert returns a registry.Verdict (uniform substrate contract)."""
    repo, _tracker, pos = store_with_chain(tmp_path, monkeypatch, 3)
    priv, pub = keypair(tmp_path, "env")
    keyring = [
        {"public_key": pub, "added_at_log_position": pos[0][0], "revoked_at_log_position": None}
    ]
    env = opcert.sign_opcert(TICKET, MATERIAL, MERGED, key_path=priv, kind=KIND, principal=ENV_ID)
    verdict = _verify(env, repo, pos[-1], keyring=keyring)
    assert isinstance(verdict, registry.Verdict)


# ---- kind-confusion: kind is bound into the signed subject (LLM-Review security finding) ------


def test_kind_confusion_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A cert signed for one attestation kind must NOT verify for another. The attestations map is
    keyed by the UNSIGNED manifest[0], so if the signature did not bind the kind, a cert signed as
    ``plan-review`` could be filed under ``completion-verifier`` and still verify (kind-confusion).
    Binding ``kind`` into the subject digest makes a mismatched kind fail."""
    repo, _tracker, pos = store_with_chain(tmp_path, monkeypatch, 3)
    priv, pub = keypair(tmp_path, "env")
    keyring = [
        {"public_key": pub, "added_at_log_position": pos[0][0], "revoked_at_log_position": None}
    ]
    env = opcert.sign_opcert(
        TICKET, MATERIAL, MERGED, kind="plan-review", key_path=priv, principal=ENV_ID
    )
    # Verifying with a DIFFERENT kind must fail (subject digest binds the kind).
    wrong = _verify(env, repo, pos[-1], keyring=keyring, kind="completion-verifier")
    assert wrong.verified is False
    assert wrong.verdict == "mismatch"
    # Control: verifying with the SAME kind the cert was signed for succeeds.
    right = _verify(env, repo, pos[-1], keyring=keyring, kind="plan-review")
    assert right.verified is True
    assert right.verdict == "certified"
