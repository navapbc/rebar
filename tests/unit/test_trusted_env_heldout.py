"""Held-out adversarial oracle for the trusted-environment config + required-environment check
(story 42d1 / urban-dihydric-trogon), re-expressed for Option B (story 4214).

NOT shown to the implementation subagent. Cases that separate a real implementation from one that
fakes the happy path:
* fail-open — an absent config file means "no required environment" (loader returns None), never a
  crash;
* located error — a malformed config raises an error that NAMES the file path (never a silent skip);
* environment not pinned — a required env absent from the config fails the check;
* keyid-spoof — a cert whose self-claimed keyid CLAIMS the pinned env but whose signature is by a
  DIFFERENT key FAILS (the check verifies against the PINNED key, not the cert's claimed keyid);
* era — a cert whose ONLY matching key is past its `revoked_at_log_position` (relative to the cert's
  STORAGE ANCHOR) does not certify.

Real ssh-keygen; the era cases run against a real rebar store's tickets-branch position chain.
"""

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
OTHER_ENV = "laptop@rebar.test"
TICKET = "abcd-1234-ef01-5678"
MATERIAL = "0123456789abcdef"
KIND = "completion-verifier"
MERGED = "0" * 40
# A syntactically valid log position that need not resolve (used where verification short-circuits
# before the era check, so the anchor is never consulted).
DUMMY_POS = "1700000000000000000-00000000-0000-0000-0000-000000000000"
DUMMY_COMMIT = "0" * 40


def _bare_repo(tmp_path: Path) -> Path:
    """A plain directory for config-only tests (loader reads the file; no store needed)."""
    repo = tmp_path / "code"
    repo.mkdir()
    return repo


def _write_config_raw(repo: Path, body: str) -> None:
    rebar_dir = repo / ".rebar"
    rebar_dir.mkdir(exist_ok=True)
    (rebar_dir / "trusted_environments.yaml").write_text(body, encoding="utf-8")


def _write_env(repo: Path, env_id: str, pub: str, added_at: str, revoked_at: str = "null") -> None:
    _write_config_raw(
        repo,
        "environments:\n"
        f"  - env_id: {env_id}\n"
        "    keys:\n"
        f"      - public_key: {pub}\n"
        f"        added_at_log_position: {added_at}\n"
        f"        revoked_at_log_position: {revoked_at}\n",
    )


# ---- fail-open / located error (AC1) ----------------------------------------------------------


def test_absent_config_is_fail_open(tmp_path: Path) -> None:
    repo = _bare_repo(tmp_path)
    # No .rebar/trusted_environments.yaml written.
    assert trusted_env.load_trusted_environments(repo_root=str(repo)) is None
    assert trusted_env.trusted_env_keyring(ENV_ID, repo_root=str(repo)) is None


def test_malformed_config_raises_located_error(tmp_path: Path) -> None:
    repo = _bare_repo(tmp_path)
    _write_config_raw(repo, "environments: [ this is : not : valid yaml\n")
    with pytest.raises(trusted_env.TrustedEnvError) as exc:
        trusted_env.load_trusted_environments(repo_root=str(repo))
    # The error must NAME the offending file path (located error, not a silent skip).
    assert "trusted_environments.yaml" in str(exc.value)


def test_legacy_git_sha_schema_raises_located_error(tmp_path: Path) -> None:
    """A legacy config still using the retired git-SHA era fields (added_at_commit) must NOT be
    silently accepted — it surfaces the same located error (Option B requires log positions)."""
    repo = _bare_repo(tmp_path)
    _priv, pub = keypair(tmp_path, "env")
    _write_config_raw(
        repo,
        "environments:\n"
        f"  - env_id: {ENV_ID}\n"
        "    keys:\n"
        f"      - public_key: {pub}\n"
        "        added_at_commit: deadbeef\n"
        "        revoked_at_commit: null\n",
    )
    with pytest.raises(trusted_env.TrustedEnvError) as exc:
        trusted_env.load_trusted_environments(repo_root=str(repo))
    assert "trusted_environments.yaml" in str(exc.value)


# ---- environment not pinned (AC2) -------------------------------------------------------------


def test_required_env_not_pinned_fails(tmp_path: Path) -> None:
    repo = _bare_repo(tmp_path)
    priv, pub = keypair(tmp_path, "env")
    # Config pins OTHER_ENV, but the check requires ENV_ID (absent) → must fail.
    _write_env(repo, OTHER_ENV, pub, DUMMY_POS)
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
        storage_anchor_commit=DUMMY_COMMIT,
        repo_root=str(repo),
    )
    assert verdict.verified is False


# ---- keyid-spoof: verify against pinned key, NOT the cert's self-claimed keyid (AC3) ----------


def test_spoofed_keyid_with_foreign_signature_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cert whose keyid CLAIMS the pinned env ENV_ID but whose signature is by a DIFFERENT
    (attacker) key must FAIL — the check verifies the signature against the PINNED key, so a
    self-claimed keyid proves nothing."""
    repo, _tracker, pos = store_with_chain(tmp_path, monkeypatch, 3)
    attacker_priv, _attacker_pub = keypair(tmp_path, "attacker")
    _pinned_priv, pinned_pub = keypair(tmp_path, "pinned")
    _write_env(repo, ENV_ID, pinned_pub, pos[0][0])

    # Attacker signs with their own key but stamps the cert's keyid/principal as the pinned env.
    envelope = opcert.sign_opcert(
        TICKET, MATERIAL, MERGED, key_path=attacker_priv, kind=KIND, principal=ENV_ID
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
    assert verdict.verified is False


def test_correct_pinned_key_still_certifies_as_control(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Contrast control for the spoof test: the SAME setup but signed by the genuine pinned key
    certifies — proving the spoof test fails for the right reason (wrong key), not a broken path."""
    repo, _tracker, pos = store_with_chain(tmp_path, monkeypatch, 3)
    pinned_priv, pinned_pub = keypair(tmp_path, "pinned")
    _write_env(repo, ENV_ID, pinned_pub, pos[0][0])

    envelope = opcert.sign_opcert(
        TICKET, MATERIAL, MERGED, key_path=pinned_priv, kind=KIND, principal=ENV_ID
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


# ---- era rotation flows through (AC2/AC3 interplay) -------------------------------------------


def test_pinned_key_past_rotation_does_not_certify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pinned key revoked at an early log position, with the cert's STORAGE ANCHOR a LATER
    tickets-branch commit, does not certify (the era rule from 368c flows through the
    required-environment check, now anchored at the storage anchor per Option B)."""
    repo, _tracker, pos = store_with_chain(tmp_path, monkeypatch, 3)
    priv, pub = keypair(tmp_path, "env")
    # Revoked at pos[1], while the storage anchor is pos[-1] (a descendant of the revoke position).
    _write_env(repo, ENV_ID, pub, pos[0][0], revoked_at=pos[1][0])
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
    assert verdict.verified is False
    assert verdict.verdict == "key_not_valid_at_era"


# ---- config field (AC1) -----------------------------------------------------------------------


def test_require_environment_config_field_defaults_off(tmp_path: Path) -> None:
    """The `verify.require_environment` config field exists and defaults to unset (opt-in)."""
    from rebar._config_schema import VerifyConfig

    assert VerifyConfig().require_environment in (None, "")
