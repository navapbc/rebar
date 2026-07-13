"""Held-out adversarial oracle for the trusted-environment config + required-environment check
(story 42d1 / urban-dihydric-trogon). NOT shown to the implementation subagent.

Cases that separate a real implementation from one that fakes the happy path:
* fail-open — an absent config file means "no required environment" (loader returns None), never a
  crash;
* located error — a malformed config raises an error that NAMES the file path (never a silent skip);
* environment not pinned — a required env absent from the config fails the check;
* keyid-spoof — a cert whose self-claimed keyid CLAIMS the pinned env but whose signature is by a
  DIFFERENT key FAILS (the check verifies against the PINNED key, not the cert's claimed keyid);
* era — a cert signed by a key past its `revoked_at_commit` does not certify.

Real ssh-keygen + real temp git repos.
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
OTHER_ENV = "laptop@rebar.test"
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


def _repo_with_chain(tmp_path: Path, n: int) -> tuple[Path, list[str]]:
    repo = tmp_path / "code"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "d@e.test"], cwd=repo, check=True, capture_output=True
    )
    subprocess.run(["git", "config", "user.name", "D"], cwd=repo, check=True, capture_output=True)
    shas: list[str] = []
    for i in range(n):
        subprocess.run(
            ["git", "commit", "-q", "--allow-empty", "-m", f"c{i}"],
            cwd=repo,
            check=True,
            capture_output=True,
        )
        shas.append(
            subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
            ).stdout.strip()
        )
    return repo, shas


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
        f"        added_at_commit: {added_at}\n"
        f"        revoked_at_commit: {revoked_at}\n",
    )


# ---- fail-open / located error (AC1) ----------------------------------------------------------


def test_absent_config_is_fail_open(tmp_path: Path) -> None:
    repo, _shas = _repo_with_chain(tmp_path, 1)
    # No .rebar/trusted_environments.yaml written.
    assert trusted_env.load_trusted_environments(repo_root=str(repo)) is None
    assert trusted_env.trusted_env_keyring(ENV_ID, repo_root=str(repo)) is None


def test_malformed_config_raises_located_error(tmp_path: Path) -> None:
    repo, _shas = _repo_with_chain(tmp_path, 1)
    _write_config_raw(repo, "environments: [ this is : not : valid yaml\n")
    with pytest.raises(trusted_env.TrustedEnvError) as exc:
        trusted_env.load_trusted_environments(repo_root=str(repo))
    # The error must NAME the offending file path (located error, not a silent skip).
    assert "trusted_environments.yaml" in str(exc.value)


# ---- environment not pinned (AC2) -------------------------------------------------------------


def test_required_env_not_pinned_fails(tmp_path: Path) -> None:
    repo, shas = _repo_with_chain(tmp_path, 1)
    commit = shas[0]
    priv, pub = _keypair(tmp_path, "env")
    # Config pins OTHER_ENV, but the check requires ENV_ID (absent) → must fail.
    _write_env(repo, OTHER_ENV, pub, commit)
    envelope = opcert.sign_opcert(
        TICKET, MATERIAL, commit, key_path=priv, kind=KIND, principal=ENV_ID
    )
    verdict = trusted_env.verify_required_environment(
        envelope, TICKET, MATERIAL, commit, ENV_ID, kind=KIND, repo_root=str(repo)
    )
    assert verdict.verified is False


# ---- keyid-spoof: verify against pinned key, NOT the cert's self-claimed keyid (AC3) ----------


def test_spoofed_keyid_with_foreign_signature_fails(tmp_path: Path) -> None:
    """A cert whose keyid CLAIMS the pinned env ENV_ID but whose signature is by a DIFFERENT
    (attacker) key must FAIL — the check verifies the signature against the PINNED key, so a
    self-claimed keyid proves nothing."""
    repo, shas = _repo_with_chain(tmp_path, 1)
    commit = shas[0]
    attacker_priv, _attacker_pub = _keypair(tmp_path, "attacker")
    _pinned_priv, pinned_pub = _keypair(tmp_path, "pinned")
    _write_env(repo, ENV_ID, pinned_pub, commit)

    # Attacker signs with their own key but stamps the cert's keyid/principal as the pinned env.
    envelope = opcert.sign_opcert(
        TICKET, MATERIAL, commit, key_path=attacker_priv, kind=KIND, principal=ENV_ID
    )
    verdict = trusted_env.verify_required_environment(
        envelope, TICKET, MATERIAL, commit, ENV_ID, kind=KIND, repo_root=str(repo)
    )
    assert verdict.verified is False


def test_correct_pinned_key_still_certifies_as_control(tmp_path: Path) -> None:
    """Contrast control for the spoof test: the SAME setup but signed by the genuine pinned key
    certifies — proving the spoof test fails for the right reason (wrong key), not a broken path."""
    repo, shas = _repo_with_chain(tmp_path, 1)
    commit = shas[0]
    pinned_priv, pinned_pub = _keypair(tmp_path, "pinned")
    _write_env(repo, ENV_ID, pinned_pub, commit)

    envelope = opcert.sign_opcert(
        TICKET, MATERIAL, commit, key_path=pinned_priv, kind=KIND, principal=ENV_ID
    )
    verdict = trusted_env.verify_required_environment(
        envelope, TICKET, MATERIAL, commit, ENV_ID, kind=KIND, repo_root=str(repo)
    )
    assert verdict.verified is True
    assert verdict.verdict == "certified"


# ---- era rotation flows through (AC2/AC3 interplay) -------------------------------------------


def test_pinned_key_past_rotation_does_not_certify(tmp_path: Path) -> None:
    """A pinned key revoked at c1, with the cert bound to a later commit c2, does not certify
    (the era rule from 368c flows through the required-environment check)."""
    repo, shas = _repo_with_chain(tmp_path, 3)
    priv, pub = _keypair(tmp_path, "env")
    _write_env(repo, ENV_ID, pub, shas[0], revoked_at=shas[1])
    envelope = opcert.sign_opcert(
        TICKET, MATERIAL, shas[2], key_path=priv, kind=KIND, principal=ENV_ID
    )
    verdict = trusted_env.verify_required_environment(
        envelope, TICKET, MATERIAL, shas[2], ENV_ID, kind=KIND, repo_root=str(repo)
    )
    assert verdict.verified is False
    assert verdict.verdict == "key_not_valid_at_era"


# ---- config field (AC1) -----------------------------------------------------------------------


def test_require_environment_config_field_defaults_off(tmp_path: Path) -> None:
    """The `verify.require_environment` config field exists and defaults to unset (opt-in)."""
    from rebar._config_schema import VerifyConfig

    assert VerifyConfig().require_environment in (None, "")
