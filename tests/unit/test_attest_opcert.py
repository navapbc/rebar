"""Happy-path op-cert trust and subject-binding tests.

The implementation agent sees this suite. It verifies that an environment-signed
``rebar.opcert.v1`` certificate round-trips against the environment's pinned Ed25519 key
with matching ticket, material, and merged-log commit subjects. The held-out companion
covers cross-key rejection, replay, era rotation, and absent ``ssh-keygen``. Integration
uses real ``ssh-keygen`` and skips only when OpenSSH 8.9 or newer is unavailable.

Held-out oracles merged from test_attest_opcert_heldout.py: The suite verifies cross-key rejection
and subject-binding defenses against ticket or material replay. It evaluates key validity at the
certificate's tickets-branch storage anchor. Revoked keys produce ``key_not_valid_at_era``, foreign
keys produce ``mismatch``, and an absent ``ssh-keygen`` fails closed. Tests use a rebar store
position chain and real ``ssh-keygen`` integration.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from _opcert_helpers import keypair, store_with_chain

from rebar._store.canonical import canonical_str
from rebar.attest import dsse, opcert, registry, sshsig
from rebar.llm.plan_review.manifest import manifest_deps, manifest_material

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


def test_pre_pin_reader_authenticates_full_manifest_then_ignores_unknown_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An additive pin is inside the authenticated payload even when an old reader ignores it."""
    repo, _tracker, pos = store_with_chain(tmp_path, monkeypatch, 3)
    priv, pub = keypair(tmp_path, "pre-pin-reader")
    keyring = [
        {"public_key": pub, "added_at_log_position": pos[0][0], "revoked_at_log_position": None}
    ]
    manifest = [
        "plan-review: PASS",
        f"material: {MATERIAL}",
        "plan-material-pin: child aaaa-bbbb-cccc-dddd fedcba9876543210",
        "dep digest src/rebar/example.py",
    ]
    envelope = opcert.sign_opcert(
        TICKET,
        MATERIAL,
        MERGED,
        key_path=priv,
        kind="plan-review",
        principal=ENV_ID,
        manifest=manifest,
    )

    verified = opcert.verify_opcert(
        envelope,
        TICKET,
        MATERIAL,
        MERGED,
        keyring,
        kind="plan-review",
        principal=ENV_ID,
        storage_anchor_commit=pos[-1][1],
        storage_anchor_position=pos[-1][0],
        repo_root=str(repo),
    )
    assert verified.verified is True

    statement = json.loads(envelope.payload)
    signed_manifest = statement["predicate"]["manifest"]
    assert signed_manifest == manifest
    # A pre-feature reader interprets only its known fields after verification.
    assert manifest_material(signed_manifest) == MATERIAL
    assert manifest_deps(signed_manifest) == {"src/rebar/example.py": "digest"}

    # Filtering the unknown pin before authentication changes the signed payload.
    statement["predicate"]["manifest"] = [
        line for line in signed_manifest if not line.startswith("plan-material-pin:")
    ]
    filtered = dsse.Envelope(
        envelope.payload_type,
        canonical_str(statement).encode(),
        envelope.signatures,
    )
    rejected = opcert.verify_opcert(
        filtered,
        TICKET,
        MATERIAL,
        MERGED,
        keyring,
        kind="plan-review",
        principal=ENV_ID,
        storage_anchor_commit=pos[-1][1],
        storage_anchor_position=pos[-1][0],
        repo_root=str(repo),
    )
    assert rejected.verified is False


# ── held-out oracles (merged from test_attest_opcert_heldout.py) ──

try:
    sshsig.ensure_available()
    _SSH_OK = True
except Exception:  # noqa: BLE001
    _SSH_OK = False


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
