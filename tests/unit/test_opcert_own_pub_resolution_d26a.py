"""Verification resolves the public half where the SIGNING key is (bug d26a-8ffa-97cd-4b2a).

The deployed MCP server signs its op-certs under a startup-composed binding whose private key is
a process-owned copy of a bind-mounted deployment secret — never ``<tracker>/.opcert-key``. The
verify side read the public key ONLY from the tracker, so a box with no tracker genesis key could
not certify the certs it had just minted: every ``verify_signature`` returned ``foreign_key``
("this environment has no op-cert public key"), which fails the plan-review CLAIM gate closed.

These tests pin the invariant that closes the loop: whatever private key the signing seam
resolves (bound startup signer → ``REBAR_OPCERT_KEY_PATH`` override → tracker genesis), the
verify side finds a public counterpart for it — including when the key sits on a READ-ONLY
secrets mount where no ``.pub`` cache can ever be written.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
from rebar import signing
from rebar._opcert_binding import bound_signer
from rebar.attest import sshsig

try:
    sshsig.ensure_available()
    _SSH_OK = True
except Exception:  # noqa: BLE001
    _SSH_OK = False

pytestmark = pytest.mark.skipif(not _SSH_OK, reason="ssh-keygen >= 8.9 required for SSHSIG")

KIND = "plan-review"
ENV_ID = "9f1c8e42-7a3b-4d5e-b6c1-2f0a9d8e7c65"


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A store whose tracker has NO ``.opcert-key`` — the deployed box's shape after a re-clone."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("init", "-q"),
        ("config", "user.email", "t@e.test"),
        ("config", "user.name", "t"),
        ("commit", "-q", "--allow-empty", "-m", "i"),
    ):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    monkeypatch.delenv("REBAR_OPCERT_KEY_PATH", raising=False)
    monkeypatch.setenv("REBAR_OPCERT_ENV_ID", ENV_ID)
    rebar.init_repo(repo_root=str(repo))
    return repo


def _tracker(store: Path) -> Path:
    from rebar._commands._seam import tracker_dir

    return Path(tracker_dir(str(store)))


def _deployment_key(tmp_path: Path, name: str = "opcert-ed25519-key") -> Path:
    """A key materialized OUTSIDE the tracker, as the deployment bind-mounts it."""
    d = tmp_path / "secrets"
    d.mkdir(exist_ok=True)
    key = d / name
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(key), "-N", "", "-q", "-C", "rebar-opcert"],
        check=True,
        capture_output=True,
    )
    key.chmod(0o600)
    (d / f"{name}.pub").unlink()  # the deployment ships the PRIVATE half only
    return key


class _Signer:
    """The ``OpcertBinding`` duck type the startup composition produces."""

    def __init__(self, key_path: Path, principal: str) -> None:
        self._key_path = str(key_path)
        self._principal = principal

    @property
    def key_path(self) -> str:
        return self._key_path

    @property
    def principal(self) -> str | None:
        return self._principal


def _sign_and_verify(store: Path, tid: str) -> dict:
    signing.sign_manifest(tid, [f"{KIND}: PASS"], kind=KIND, repo_root=str(store))
    return signing.verify_signature(tid, kind=KIND, repo_root=str(store))


def test_bound_signer_cert_certifies_in_the_same_environment(store: Path, tmp_path: Path) -> None:
    """The deployed shape: a startup binding signs, and the SAME server verifies. Before the fix
    the verify side looked only in the (keyless) tracker and returned ``foreign_key``, so the
    claim gate refused every ticket."""
    key = _deployment_key(tmp_path)
    tid = rebar.create_ticket("task", "bound", repo_root=str(store))
    with bound_signer(_Signer(key, ENV_ID), push_mode=None):
        result = _sign_and_verify(store, tid)
    assert result["verdict"] == "certified", result.get("reason")
    assert result["verified"] is True
    # Verification stays read-only: it must not mint a private key into the tracker.
    assert not (_tracker(store) / ".opcert-key").exists()


def test_key_path_override_cert_certifies_in_the_same_environment(
    store: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unbound deployment override: ``REBAR_OPCERT_KEY_PATH`` selects the signing key, so it
    must select the verification key too."""
    key = _deployment_key(tmp_path)
    monkeypatch.setenv("REBAR_OPCERT_KEY_PATH", str(key))
    tid = rebar.create_ticket("task", "override", repo_root=str(store))
    result = _sign_and_verify(store, tid)
    assert result["verdict"] == "certified", result.get("reason")
    assert not (_tracker(store) / ".opcert-key").exists()


def test_certifies_when_the_key_dir_is_read_only(
    store: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bind-mounted secret is not writable, so the ``.pub`` cache can never be created next to
    it. The public line must be derived in memory rather than round-tripped through a file."""
    key = _deployment_key(tmp_path)
    monkeypatch.setenv("REBAR_OPCERT_KEY_PATH", str(key))
    tid = rebar.create_ticket("task", "readonly", repo_root=str(store))
    key.parent.chmod(0o500)
    try:
        result = _sign_and_verify(store, tid)
    finally:
        key.parent.chmod(0o700)
    assert result["verdict"] == "certified", result.get("reason")
    assert not (key.parent / f"{key.name}.pub").exists(), "no cache is writable here"


def test_tracker_genesis_still_certifies_and_caches_its_pub(store: Path) -> None:
    """Regression oracle: with no binding and no override, the unbound genesis path is unchanged
    — the tracker key is created on first sign and its ``.pub`` cache is written."""
    tid = rebar.create_ticket("task", "genesis", repo_root=str(store))
    result = _sign_and_verify(store, tid)
    assert result["verdict"] == "certified", result.get("reason")
    assert (_tracker(store) / ".opcert-key.pub").exists()
