"""Held-out adversarial oracle for the op-cert kind (story 368c / garlicky-deviant-kakapo).

NOT shown to the implementation subagent. These are the cases that separate a real op-cert
implementation from one that fakes the happy path:

* cross-key rejection — a cert verifies ONLY against the signing environment's key;
* replay defense — the {ticket, material, merged-log commit} subject binding rejects a cert
  replayed onto a different ticket or onto a mutated material fingerprint;
* explicit-SHA era rotation — a key valid at the cert's bound commit verifies; one past its
  ``revoked_at_commit`` surfaces the shared ``key_not_valid_at_era`` verdict (two-phase check);
  a genuinely foreign key surfaces ``mismatch``, not ``key_not_valid_at_era``;
* fail-closed — ssh-keygen absent never silently passes.

Real ``ssh-keygen`` integration + real temp git repos for the ancestry rule.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

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
    """A real git repo with ``n`` sequential commits; return (repo, [sha0, sha1, ...])."""
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


# ---- cross-key rejection (AC1) ----------------------------------------------------------------


def test_verify_fails_against_different_key(tmp_path: Path) -> None:
    repo, shas = _repo_with_chain(tmp_path, 1)
    commit = shas[0]
    priv, _pub = _keypair(tmp_path, "env")
    _other_priv, other_pub = _keypair(tmp_path, "other")
    # keyring pins a DIFFERENT environment key than the one that signed.
    keyring = [{"public_key": other_pub, "added_at_commit": commit, "revoked_at_commit": None}]

    env = opcert.sign_opcert(TICKET, MATERIAL, commit, key_path=priv, principal=ENV_ID)
    verdict = opcert.verify_opcert(
        env, TICKET, MATERIAL, commit, keyring, principal=ENV_ID, repo_root=str(repo)
    )
    assert verdict.verified is False
    assert verdict.verdict == "mismatch"


# ---- replay defense via subject binding (AC2) -------------------------------------------------


def test_replay_onto_different_ticket_rejected(tmp_path: Path) -> None:
    repo, shas = _repo_with_chain(tmp_path, 1)
    commit = shas[0]
    priv, pub = _keypair(tmp_path, "env")
    keyring = [{"public_key": pub, "added_at_commit": commit, "revoked_at_commit": None}]

    env = opcert.sign_opcert(TICKET, MATERIAL, commit, key_path=priv, principal=ENV_ID)
    # Same, validly-signed envelope, presented as if it certified a DIFFERENT ticket.
    verdict = opcert.verify_opcert(
        env, "9999-0000-0000-0000", MATERIAL, commit, keyring, principal=ENV_ID, repo_root=str(repo)
    )
    assert verdict.verified is False
    assert verdict.verdict in {"subject_mismatch", "mismatch"}


def test_replay_onto_mutated_material_rejected(tmp_path: Path) -> None:
    repo, shas = _repo_with_chain(tmp_path, 1)
    commit = shas[0]
    priv, pub = _keypair(tmp_path, "env")
    keyring = [{"public_key": pub, "added_at_commit": commit, "revoked_at_commit": None}]

    env = opcert.sign_opcert(TICKET, MATERIAL, commit, key_path=priv, principal=ENV_ID)
    # The ticket's material changed after signing → the cert must no longer verify for it.
    verdict = opcert.verify_opcert(
        env, TICKET, "ffffffffffffffff", commit, keyring, principal=ENV_ID, repo_root=str(repo)
    )
    assert verdict.verified is False
    assert verdict.verdict in {"subject_mismatch", "mismatch"}


# ---- explicit-SHA era rotation + two-phase verdict (AC3) --------------------------------------


def test_key_valid_at_era_verifies(tmp_path: Path) -> None:
    """Key added at c0, not revoked; cert bound to a later commit c2 → the add-commit is an
    ancestor of the bound commit, so the key is valid at that era and the cert verifies."""
    repo, shas = _repo_with_chain(tmp_path, 3)
    priv, pub = _keypair(tmp_path, "env")
    keyring = [{"public_key": pub, "added_at_commit": shas[0], "revoked_at_commit": None}]

    env = opcert.sign_opcert(TICKET, MATERIAL, shas[2], key_path=priv, principal=ENV_ID)
    verdict = opcert.verify_opcert(
        env, TICKET, MATERIAL, shas[2], keyring, principal=ENV_ID, repo_root=str(repo)
    )
    assert verdict.verified is True
    assert verdict.verdict == "certified"


def test_key_past_rotation_surfaces_key_not_valid_at_era(tmp_path: Path) -> None:
    """Key added at c0 and revoked at c1; cert bound to a LATER commit c2 (revoke-commit is an
    ancestor of the bound commit) → the ONLY matching key is past its rotation. The signature is
    genuinely by that (historical) key, so the two-phase check must surface key_not_valid_at_era,
    NOT mismatch."""
    repo, shas = _repo_with_chain(tmp_path, 3)
    priv, pub = _keypair(tmp_path, "env")
    keyring = [{"public_key": pub, "added_at_commit": shas[0], "revoked_at_commit": shas[1]}]

    env = opcert.sign_opcert(TICKET, MATERIAL, shas[2], key_path=priv, principal=ENV_ID)
    verdict = opcert.verify_opcert(
        env, TICKET, MATERIAL, shas[2], keyring, principal=ENV_ID, repo_root=str(repo)
    )
    assert verdict.verified is False
    assert verdict.verdict == "key_not_valid_at_era"


def test_key_not_yet_added_surfaces_key_not_valid_at_era(tmp_path: Path) -> None:
    """Key added at c2, but cert bound to an EARLIER commit c0 (add-commit is NOT an ancestor of
    the bound commit) → key not yet valid at that era; signature is real → key_not_valid_at_era."""
    repo, shas = _repo_with_chain(tmp_path, 3)
    priv, pub = _keypair(tmp_path, "env")
    keyring = [{"public_key": pub, "added_at_commit": shas[2], "revoked_at_commit": None}]

    env = opcert.sign_opcert(TICKET, MATERIAL, shas[0], key_path=priv, principal=ENV_ID)
    verdict = opcert.verify_opcert(
        env, TICKET, MATERIAL, shas[0], keyring, principal=ENV_ID, repo_root=str(repo)
    )
    assert verdict.verified is False
    assert verdict.verdict == "key_not_valid_at_era"


def test_foreign_key_surfaces_mismatch_not_era(tmp_path: Path) -> None:
    """A cert signed by a key that is NOT in the keyring at all (nor any era) → mismatch,
    never key_not_valid_at_era (the two-phase any-historical-key pass must fail first)."""
    repo, shas = _repo_with_chain(tmp_path, 1)
    commit = shas[0]
    foreign_priv, _ = _keypair(tmp_path, "foreign")
    _, pinned_pub = _keypair(tmp_path, "pinned")
    keyring = [{"public_key": pinned_pub, "added_at_commit": commit, "revoked_at_commit": None}]

    env = opcert.sign_opcert(TICKET, MATERIAL, commit, key_path=foreign_priv, principal=ENV_ID)
    verdict = opcert.verify_opcert(
        env, TICKET, MATERIAL, commit, keyring, principal=ENV_ID, repo_root=str(repo)
    )
    assert verdict.verified is False
    assert verdict.verdict == "mismatch"


def test_empty_keyring_does_not_verify(tmp_path: Path) -> None:
    repo, shas = _repo_with_chain(tmp_path, 1)
    commit = shas[0]
    priv, _pub = _keypair(tmp_path, "env")
    env = opcert.sign_opcert(TICKET, MATERIAL, commit, key_path=priv, principal=ENV_ID)
    verdict = opcert.verify_opcert(
        env, TICKET, MATERIAL, commit, [], principal=ENV_ID, repo_root=str(repo)
    )
    assert verdict.verified is False


# ---- fail-closed (never a silent pass) --------------------------------------------------------


def test_ssh_keygen_absent_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If the verifier tool is unavailable, verify must fail closed — never a silent pass."""
    repo, shas = _repo_with_chain(tmp_path, 1)
    commit = shas[0]
    priv, pub = _keypair(tmp_path, "env")
    keyring = [{"public_key": pub, "added_at_commit": commit, "revoked_at_commit": None}]
    env = opcert.sign_opcert(TICKET, MATERIAL, commit, key_path=priv, principal=ENV_ID)

    monkeypatch.setattr(sshsig, "ssh_keygen_version", lambda: None)
    verdict = opcert.verify_opcert(
        env, TICKET, MATERIAL, commit, keyring, principal=ENV_ID, repo_root=str(repo)
    )
    assert verdict.verified is False


def test_verdict_type_is_registry_verdict(tmp_path: Path) -> None:
    """verify_opcert returns a registry.Verdict (uniform substrate contract)."""
    repo, shas = _repo_with_chain(tmp_path, 1)
    commit = shas[0]
    priv, pub = _keypair(tmp_path, "env")
    keyring = [{"public_key": pub, "added_at_commit": commit, "revoked_at_commit": None}]
    env = opcert.sign_opcert(TICKET, MATERIAL, commit, key_path=priv, principal=ENV_ID)
    verdict = opcert.verify_opcert(
        env, TICKET, MATERIAL, commit, keyring, principal=ENV_ID, repo_root=str(repo)
    )
    assert isinstance(verdict, registry.Verdict)
