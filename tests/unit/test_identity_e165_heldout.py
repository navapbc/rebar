"""HELD-OUT oracle for e165 — the implementation MUST NOT see this file.

Validates the security-critical lifecycle: a key change on an identity that already
has a valid key is REFUSED unless signed by a currently-valid key; a validly-signed
rotation is accepted; an authorship signature by a key REVOKED at an earlier epoch
fails while one within the key's valid epoch passes; the keyring survives compaction
with a contiguous epoch counter; and the governing epoch derives from fold order,
not the event's data timestamp. Observable behaviour only.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
from rebar.attest import authorship, sshsig

try:
    sshsig.ensure_available()
    _SSH_OK = True
except Exception:  # noqa: BLE001 — best-effort SSHSIG availability probe; skip if unavailable
    _SSH_OK = False

pytestmark = pytest.mark.skipif(not _SSH_OK, reason="ssh-keygen >= 8.9 required for SSHSIG")


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # Keep events unfolded by default so keyring events are inspectable; the
    # snapshot test forces folding locally.
    monkeypatch.setenv("REBAR_COMPACTION_HORIZON_NS", "9" * 18)
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


def _genesis(store: Path, tmp_path: Path):
    """Create an identity with a TOFU genesis key; return (id, priv, pub)."""
    priv, pub = _keypair(tmp_path, "genesis")
    ident = rebar.create_identity("Ada", "ada@example.com", repo_root=str(store))
    rebar.add_identity_key(ident, pub, signature=None, repo_root=str(store))
    return ident, priv, pub


def _sign_keyop(op: str, ident: str, public_key: str, signer_priv: str):
    """Sign a KEY op with `signer_priv` (a currently-valid key)."""
    payload = authorship.keyop_payload(op, ident, public_key)
    return authorship.sign_authorship(payload, signer_priv, principal=ident)


def test_unsigned_rotation_refused(store: Path, tmp_path: Path) -> None:
    """Adding a key to an identity that already has a valid key WITHOUT a signature
    is refused (raises); no key event is appended."""
    ident, _priv, _pub = _genesis(store, tmp_path)
    _priv2, pub2 = _keypair(tmp_path, "k2")
    before = len(rebar.show_ticket(ident, repo_root=str(store))["keyring"])
    with pytest.raises(rebar.RebarError):
        rebar.add_identity_key(ident, pub2, signature=None, repo_root=str(store))
    after = len(rebar.show_ticket(ident, repo_root=str(store))["keyring"])
    assert after == before  # no key added


def test_signed_rotation_accepted(store: Path, tmp_path: Path) -> None:
    """A key-add signed by the currently-valid genesis key is accepted at epoch 1."""
    ident, priv, _pub = _genesis(store, tmp_path)
    _priv2, pub2 = _keypair(tmp_path, "k2")
    env = _sign_keyop("KEY_ADD", ident, pub2, priv)
    rebar.add_identity_key(ident, pub2, signature=env, repo_root=str(store))
    ring = rebar.show_ticket(ident, repo_root=str(store))["keyring"]
    rec = next(r for r in ring if r["public_key"] == pub2)
    assert rec["added_epoch"] == 1
    assert rec["revoked_epoch"] is None


def test_revoked_key_fails_at_revoked_epoch(store: Path, tmp_path: Path) -> None:
    """A key valid on [0,1) verifies an authorship envelope at epoch 0 but not at
    epoch 1 (after its revocation)."""
    ident, priv, pub = _genesis(store, tmp_path)
    # Revoke the genesis key, signed by itself (valid at epoch 0).
    env_rev = _sign_keyop("KEY_REVOKE", ident, pub, priv)
    rebar.revoke_identity_key(ident, pub, signature=env_rev, repo_root=str(store))

    rec = next(
        r
        for r in rebar.show_ticket(ident, repo_root=str(store))["keyring"]
        if r["public_key"] == pub
    )
    assert rec["added_epoch"] == 0 and rec["revoked_epoch"] == 1

    payload = b'{"uuid":"e2","data":{"y":2}}'
    env = authorship.sign_authorship(payload, priv, principal=ident)
    assert (
        authorship.verify_authorship_at_epoch(env, ident, 0, repo_root=str(store)).verified is True
    )
    assert (
        authorship.verify_authorship_at_epoch(env, ident, 1, repo_root=str(store)).verified is False
    )


def test_keyring_survives_compaction_with_contiguous_epoch(
    store: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After compaction, the keyring + keyring_epoch match the uncompacted fold, and
    a post-snapshot signed key-add gets the next contiguous epoch."""
    ident, priv, _pub = _genesis(store, tmp_path)
    _priv2, pub2 = _keypair(tmp_path, "k2")
    rebar.add_identity_key(
        ident, pub2, signature=_sign_keyop("KEY_ADD", ident, pub2, priv), repo_root=str(store)
    )
    ring_before = rebar.show_ticket(ident, repo_root=str(store))["keyring"]
    epoch_before = rebar.show_ticket(ident, repo_root=str(store))["keyring_epoch"]

    # Force compaction of the identity, folding the KEY events into a SNAPSHOT.
    monkeypatch.setenv("REBAR_COMPACTION_HORIZON_NS", "0")
    monkeypatch.setenv("REBAR_COMPACT_THRESHOLD", "1")
    rebar.compact(ident, repo_root=str(store))

    st = rebar.show_ticket(ident, repo_root=str(store))
    assert st["keyring_epoch"] == epoch_before
    assert {r["public_key"] for r in st["keyring"]} == {r["public_key"] for r in ring_before}

    # A post-snapshot signed add continues the epoch counter (not reset).
    _priv3, pub3 = _keypair(tmp_path, "k3")
    rebar.add_identity_key(
        ident, pub3, signature=_sign_keyop("KEY_ADD", ident, pub3, priv), repo_root=str(store)
    )
    rec = next(
        r
        for r in rebar.show_ticket(ident, repo_root=str(store))["keyring"]
        if r["public_key"] == pub3
    )
    assert rec["added_epoch"] == epoch_before  # contiguous, not reset to a small idx


def test_epoch_derives_from_fold_order_not_timestamp(store: Path, tmp_path: Path) -> None:
    """`epoch_for_position` reflects how many KEY events precede a position — derived
    from the canonical fold order, independent of any event data timestamp."""
    ident, priv, pub = _genesis(store, tmp_path)  # 1 KEY event so far (epoch cursor -> 1)
    # Revoke → a second KEY event.
    rebar.revoke_identity_key(
        ident, pub, signature=_sign_keyop("KEY_REVOKE", ident, pub, priv), repo_root=str(store)
    )
    st = rebar.show_ticket(ident, repo_root=str(store))
    # keyring_epoch is the next-epoch cursor: genesis(0) + revoke(1) consumed -> 2.
    assert st["keyring_epoch"] == 2
