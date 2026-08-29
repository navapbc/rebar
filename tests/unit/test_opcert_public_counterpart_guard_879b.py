"""Guards that surface a signing key with no usable public counterpart (bug 879b-9bf0-86fd-4a6b).

Split from ``d26a-8ffa-97cd-4b2a`` (``dire-expectable-terrapin``), which fixed the key-resolution
DIVERGENCE (Gerrit 2337). These two ADDITIVE, NON-REFUSING guards surface the class if it ever
regresses under a new deployment shape:

  * AC1/AC2 — the mint path (``sign_manifest`` -> ``mint_opcert_record``) emits a WARNING naming
    the resolved key path and the principal when the SAME-ENVIRONMENT verify resolver
    (``_opcert_own_public_keys``) cannot resolve a public counterpart for the key it just signed
    under, and STILL returns the signature (no refusal, no existing sign flow broken).
  * AC3 — the MCP server's startup health surface reports a DEGRADED field and logs a warning when
    the bound startup signer's public key is NOT among the pinned trusted-environment keys for its
    principal (the one failure the derived-key same-env verify path cannot catch). Non-blocking: it
    NEVER aborts boot (ADR 0104 decision 3 — required-environment binding is advisory today).
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import pytest

import rebar
from rebar import _mcp_health, signing
from rebar.attest import sshsig

try:
    sshsig.ensure_available()
    _SSH_OK = True
except Exception:  # noqa: BLE001
    _SSH_OK = False

pytestmark = pytest.mark.skipif(not _SSH_OK, reason="ssh-keygen >= 8.9 required for SSHSIG")

ENV_ID = "9f1c8e42-7a3b-4d5e-b6c1-2f0a9d8e7c65"
KIND = "plan-review"


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
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


def _keypair(tmp_path: Path, name: str) -> Path:
    """An ed25519 keypair with its ``.pub`` KEPT beside the private key (a normal signer key)."""
    d = tmp_path / "keys"
    d.mkdir(exist_ok=True)
    key = d / name
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(key), "-N", "", "-q", "-C", "rebar-opcert"],
        check=True,
        capture_output=True,
    )
    key.chmod(0o600)
    return key


def _pub_of(key: Path) -> str:
    return Path(str(key) + ".pub").read_text(encoding="utf-8").strip()


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


def _write_trusted_env(store: Path, env_id: str, pub_line: str) -> None:
    d = store / ".rebar"
    d.mkdir(exist_ok=True)
    (d / "trusted_environments.yaml").write_text(
        "environments:\n"
        f'  - env_id: "{env_id}"\n'
        "    keys:\n"
        f'      - public_key: "{pub_line}"\n'
        '        added_at_log_position: "0-genesis"\n'
        "        revoked_at_log_position: null\n",
        encoding="utf-8",
    )


# ── AC1 / AC2 — mint-time warning ─────────────────────────────────────────────


def test_mint_warns_when_verify_cannot_resolve_public_counterpart(
    store: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """AC1: when the same-environment verify resolver returns no public counterpart for the key
    just signed under (the regression class this guard defends against), the mint WARNS — naming
    the resolved key path and the principal — and STILL returns a signature (non-refusing)."""
    monkeypatch.setattr("rebar._opcert_signing._opcert_own_public_keys", lambda tracker: [])
    tid = rebar.create_ticket("task", "warn", repo_root=str(store))
    with caplog.at_level(logging.WARNING, logger="rebar._opcert_signing"):
        rec = signing.sign_manifest(tid, [f"{KIND}: PASS"], kind=KIND, repo_root=str(store))

    assert rec.get("algorithm") == "sshsig"
    assert rec.get("envelope"), "non-refusing: a signature must still be returned"

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    hits = [m for m in warnings if "public counterpart" in m]
    assert hits, warnings
    tracker = str(_tracker(store))
    assert any(tracker in m for m in hits), (tracker, hits)
    assert any(ENV_ID in m for m in hits), (ENV_ID, hits)


def test_mint_warns_when_signed_key_pub_absent_though_another_chain_key_resolves(
    store: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """AC1 specific-key contract (G6, Gerrit 2360 plan-review): the guard checks the SPECIFIC
    signing key's public counterpart, NOT mere non-emptiness of the aggregate own-key chain. When
    a DIFFERENT chain key's pub resolves (aggregate NON-EMPTY) but the signed key's own public
    half is absent from the resolvable set, the mint must still WARN — a non-emptiness check would
    stay wrongly silent here."""
    other = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOtherChainKeyBodyNotTheSigner999 other@elsewhere"
    monkeypatch.setattr("rebar._opcert_signing._opcert_own_public_keys", lambda tracker: [other])
    tid = rebar.create_ticket("task", "warn-specific", repo_root=str(store))
    with caplog.at_level(logging.WARNING, logger="rebar._opcert_signing"):
        rec = signing.sign_manifest(tid, [f"{KIND}: PASS"], kind=KIND, repo_root=str(store))

    assert rec.get("envelope"), "non-refusing: a signature must still be returned"
    hits = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and "public counterpart" in r.getMessage()
    ]
    assert hits, "must warn: the signing key's OWN public half is not in the resolvable set"
    assert any(ENV_ID in m for m in hits), (ENV_ID, hits)


def test_mint_emits_no_warning_when_public_counterpart_resolvable(
    store: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """AC2: the ordinary genesis path resolves its OWN public counterpart, so no warning fires."""
    tid = rebar.create_ticket("task", "quiet", repo_root=str(store))
    with caplog.at_level(logging.WARNING, logger="rebar._opcert_signing"):
        rec = signing.sign_manifest(tid, [f"{KIND}: PASS"], kind=KIND, repo_root=str(store))

    assert rec.get("algorithm") == "sshsig"
    hits = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and "public counterpart" in r.getMessage()
    ]
    assert hits == [], hits


# ── AC3 — startup/deploy serve-degraded health check (never aborts) ───────────


def test_opcert_status_unbound_is_not_degraded() -> None:
    """No startup signer (dev / stdio) — nothing to check, nothing degraded."""
    status = _mcp_health.opcert_signing_status(None, repo_root=None)
    assert status["bound"] is False
    assert _mcp_health.opcert_signer_degraded(status) is False


def test_opcert_status_bound_without_pinning_config_is_not_degraded(
    store: Path, tmp_path: Path
) -> None:
    """`expected` gates strictness like store_status: a deployment that has not configured
    trusted-environment pinning is not marked degraded (required-env verify is advisory today)."""
    signer = _Signer(_keypair(tmp_path, "k1"), ENV_ID)
    status = _mcp_health.opcert_signing_status(signer, repo_root=str(store))
    assert status["bound"] is True
    assert status["expected"] is False
    assert _mcp_health.opcert_signer_degraded(status) is False


def test_opcert_status_matched_when_signer_pub_is_pinned(store: Path, tmp_path: Path) -> None:
    key = _keypair(tmp_path, "k2")
    _write_trusted_env(store, ENV_ID, _pub_of(key))
    status = _mcp_health.opcert_signing_status(_Signer(key, ENV_ID), repo_root=str(store))
    assert status["expected"] is True
    assert status["matched"] is True
    assert _mcp_health.opcert_signer_degraded(status) is False


def test_opcert_status_degraded_when_signer_pub_not_pinned(store: Path, tmp_path: Path) -> None:
    """A valid signer key whose public half is NOT the pinned one — the wrong/unpublished-key
    failure the derived-key path cannot catch. Degraded, but still serving."""
    key = _keypair(tmp_path, "k3")
    _write_trusted_env(store, ENV_ID, _pub_of(_keypair(tmp_path, "other")))
    status = _mcp_health.opcert_signing_status(_Signer(key, ENV_ID), repo_root=str(store))
    assert status["expected"] is True
    assert status["matched"] is False
    assert _mcp_health.opcert_signer_degraded(status) is True


def test_startup_opcert_check_warns_only_when_degraded(
    store: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The startup check logs a warning (naming the principal) when degraded, is silent when the
    signer is pinned, and NEVER raises (it must not abort boot)."""
    key = _keypair(tmp_path, "k4")
    _write_trusted_env(store, ENV_ID, _pub_of(_keypair(tmp_path, "o2")))
    with caplog.at_level(logging.WARNING, logger="rebar"):
        _mcp_health.run_startup_opcert_check(_Signer(key, ENV_ID), repo_root=str(store))
    degraded_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(ENV_ID in m for m in degraded_msgs), degraded_msgs
    assert any(("pinned" in m or "trusted-environment" in m) for m in degraded_msgs), degraded_msgs

    caplog.clear()
    _write_trusted_env(store, ENV_ID, _pub_of(key))  # now the signer IS pinned
    with caplog.at_level(logging.WARNING, logger="rebar"):
        _mcp_health.run_startup_opcert_check(_Signer(key, ENV_ID), repo_root=str(store))
    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []


def test_opcert_status_matched_ignores_comment_field(store: Path, tmp_path: Path) -> None:
    """Match is on the key's type+base64 BODY, not the full ``.pub`` line: a pinned key that
    differs from the signer's ``.pub`` ONLY in its trailing comment is the SAME key and must match
    — ssh's allowed_signers verify (the required-environment path) ignores the comment field
    (Gerrit 2360 review). A body-blind full-line compare would falsely report DEGRADED here."""
    key = _keypair(tmp_path, "kc")
    typ, body, *_ = _pub_of(key).split()
    _write_trusted_env(store, ENV_ID, f"{typ} {body} a-different-comment@elsewhere")
    status = _mcp_health.opcert_signing_status(_Signer(key, ENV_ID), repo_root=str(store))
    assert status["expected"] is True
    assert status["matched"] is True
    assert _mcp_health.opcert_signer_degraded(status) is False


def test_opcert_status_reports_error_and_is_degraded_on_resolution_fault(
    store: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A genuine resolution fault after opting into pinning (a config load failure) records an
    ``error`` and is reported DEGRADED via the error branch — a real fault must NOT be silently
    swallowed as not-degraded (Gerrit 2360 review). Patching the load fault (rather than a later
    keyring fault) isolates the error branch: ``expected`` stays False, so only the ``error`` check
    can make this degraded. The probe itself still never raises."""
    key = _keypair(tmp_path, "ke")
    _write_trusted_env(store, ENV_ID, _pub_of(key))

    def _boom(*_a: object, **_k: object) -> object:
        raise RuntimeError("trusted-env config unreadable")

    monkeypatch.setattr("rebar.attest.trusted_env.load_trusted_environments", _boom)
    status = _mcp_health.opcert_signing_status(_Signer(key, ENV_ID), repo_root=str(store))
    assert status["expected"] is False
    assert "trusted-env config unreadable" in status.get("error", "")
    assert _mcp_health.opcert_signer_degraded(status) is True


def test_opcert_status_reraises_removed_input_error(
    store: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A removed-but-still-set load-bearing input must fail HARD, not be reported as merely
    degraded (mirrors store_status; contract for the ``except RemovedInputError: raise`` guard)."""
    from rebar._deprecations import RemovedInputError

    key = _keypair(tmp_path, "kr")
    _write_trusted_env(store, ENV_ID, _pub_of(key))

    def _removed(*_a: object, **_k: object) -> object:
        raise RemovedInputError("a removed still-set input")

    monkeypatch.setattr("rebar.attest.trusted_env.trusted_env_keyring", _removed)
    with pytest.raises(RemovedInputError):
        _mcp_health.opcert_signing_status(_Signer(key, ENV_ID), repo_root=str(store))


def test_startup_opcert_check_reraises_removed_input_error(
    store: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The boot check must let a RemovedInputError fail MCP startup hard (not swallow it into a
    silent boot) — the ``except RemovedInputError: raise`` guard the broad-except contract wants."""
    from rebar._deprecations import RemovedInputError

    key = _keypair(tmp_path, "krs")
    _write_trusted_env(store, ENV_ID, _pub_of(key))

    def _removed(*_a: object, **_k: object) -> object:
        raise RemovedInputError("a removed still-set input")

    monkeypatch.setattr("rebar.attest.trusted_env.trusted_env_keyring", _removed)
    with pytest.raises(RemovedInputError):
        _mcp_health.run_startup_opcert_check(_Signer(key, ENV_ID), repo_root=str(store))
