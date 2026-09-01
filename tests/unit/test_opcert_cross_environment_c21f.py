"""Certification environment is NOT a gate (bug c21f-6f29-5d2d-4a5a).

Operator policy, recorded verbatim on the bug:

    "Certification environment should not currently be a gate. Any certification is as good
    as any other certification right now. Limited to a trusted set of environments is a
    future feature, but not currently in use."

So a cryptographically valid op-cert certifies here no matter which environment minted it —
the local CLI can consume a cert the on-box MCP server signed, and vice versa. What is NOT
loosened: the signature itself. A forged/altered envelope is still ``mismatch``, and the
opt-in ``verify.require_environment`` seam still restricts the signer when an operator sets
it.

Every cross-environment case below is built for REAL — a distinct Ed25519 key under a
distinct principal — never by mocking the principal comparison away, and each asserts the
principal genuinely differs from the verifying environment's before asserting acceptance.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
from rebar import signing
from rebar._opcert_binding import bound_signer
from rebar._store.ticket_layout import ticket_dir as layout_ticket_dir
from rebar.attest import sshsig

try:
    sshsig.ensure_available()
    _SSH_OK = True
except Exception:  # noqa: BLE001
    _SSH_OK = False

pytestmark = pytest.mark.skipif(not _SSH_OK, reason="ssh-keygen >= 8.9 required for SSHSIG")

KIND = "plan-review"
ENV_A = "9f1c8e42-7a3b-4d5e-b6c1-2f0a9d8e7c65"  # the signing environment (e.g. the MCP server)
ENV_B = "b8a354e0-1c2d-4e3f-9a8b-7c6d5e4f3a2b"  # the verifying environment (e.g. a local CLI)


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
    monkeypatch.setenv("REBAR_OPCERT_ENV_ID", ENV_B)  # this environment is B throughout
    rebar.init_repo(repo_root=str(repo))
    return repo


def _keypair(tmp_path: Path, name: str) -> Path:
    d = tmp_path / "secrets"
    d.mkdir(exist_ok=True)
    key = d / name
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(key), "-N", "", "-q", "-C", "rebar-opcert"],
        check=True,
        capture_output=True,
    )
    key.chmod(0o600)
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


def _sign_as_env_a(store: Path, tmp_path: Path, tid: str) -> Path:
    """Sign a plan-review op-cert as environment A, under A's OWN key. Returns A's key path."""
    key_a = _keypair(tmp_path, "env-a-key")
    with bound_signer(_Signer(key_a, ENV_A)):
        rec = signing.sign_manifest(tid, [f"{KIND}: PASS"], kind=KIND, repo_root=str(store))
    # ANCHOR: the cert really was minted by A, not by the verifying environment B.
    assert rec["principal"] == ENV_A
    assert rec["algorithm"] == "sshsig"
    return key_a


def _own_principal(store: Path) -> str:
    from rebar._commands._seam import tracker_dir
    from rebar._opcert_signing import opcert_principal

    return opcert_principal(str(tracker_dir(str(store))))


def _pin_environment(store: Path, env_id: str, key_path: Path) -> None:
    """Pin ``env_id``'s public key out-of-band in ``.rebar/trusted_environments.yaml``."""
    pub = Path(str(key_path) + ".pub").read_text(encoding="utf-8").strip()
    d = store / ".rebar"
    d.mkdir(exist_ok=True)
    (d / "trusted_environments.yaml").write_text(
        "environments:\n"
        f"  - env_id: {env_id!r}\n"
        "    keys:\n"
        f"      - public_key: {pub!r}\n"
        "        added_at_log_position: '0-00000000-0000-4000-8000-000000000000'\n"
        "        revoked_at_log_position: null\n",
        encoding="utf-8",
    )


def _enforce(store: Path, required_env: str) -> None:
    """Flip the opt-in enforcement pair ON (the runbook sets BOTH together)."""
    (store / "rebar.toml").write_text(
        f'[verify]\nrequire_environment = "{required_env}"\nopcert_enforce_since = "HEAD"\n',
        encoding="utf-8",
    )


def _forge_payload(store: Path, tid: str) -> None:
    """Rewrite the SIGNED payload while keeping the real signature — the content-forgery move:
    claim an attestation the signer never made. The envelope stays structurally intact (its
    embedded key still parses), so what refuses it can only be the signature check itself."""
    import base64
    import glob
    import json
    import os

    from rebar._commands._seam import tracker_dir

    tdir = Path(layout_ticket_dir(tracker_dir(str(store)), tid))
    sig_files = sorted(tdir.glob("*-SIGNATURE.json"))
    assert sig_files, "expected a SIGNATURE event to forge"
    path = sig_files[-1]
    ev = json.loads(path.read_text(encoding="utf-8"))
    env = json.loads(ev["data"]["envelope"])
    payload = json.loads(base64.b64decode(env["payload"]))
    payload["predicate"]["manifest"] = [f"{KIND}: PASS", "forged: an extra attested step"]
    env["payload"] = base64.b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).decode("ascii")
    ev["data"]["envelope"] = json.dumps(env)
    path.write_text(json.dumps(ev, ensure_ascii=False), encoding="utf-8")
    for cache in glob.glob(str(tdir / ".cache.json")):
        os.remove(cache)


def _resign_with_attacker_key(store: Path, tmp_path: Path, tid: str) -> None:
    """Re-sign the SAME payload with an ATTACKER's key while still claiming to be environment A —
    a key substitution. The pinned trust root must refuse it."""
    import glob
    import json
    import os

    from rebar._commands._seam import tracker_dir
    from rebar.attest import dsse

    attacker = _keypair(tmp_path, "attacker-key")
    tdir = Path(layout_ticket_dir(tracker_dir(str(store)), tid))
    path = sorted(tdir.glob("*-SIGNATURE.json"))[-1]
    ev = json.loads(path.read_text(encoding="utf-8"))
    envelope = dsse.decode(ev["data"]["envelope"])
    sig = subprocess.run(
        ["ssh-keygen", "-Y", "sign", "-f", str(attacker), "-n", "rebar.opcert.v1", "-q"],
        input=envelope.pae(),
        capture_output=True,
        check=True,
    ).stdout
    ev["data"]["envelope"] = dsse.encode(
        envelope.payload_type, envelope.payload, [{"keyid": ENV_A, "sig": sig}]
    )
    path.write_text(json.dumps(ev, ensure_ascii=False), encoding="utf-8")
    for cache in glob.glob(str(tdir / ".cache.json")):
        os.remove(cache)


# ── AC1: enforcement unset → a cross-environment cert certifies ───────────────
def test_cross_environment_cert_certifies_when_enforcement_unset(
    store: Path, tmp_path: Path
) -> None:
    tid = rebar.create_ticket("task", "cross-env", repo_root=str(store))
    _sign_as_env_a(store, tmp_path, tid)

    # ANCHOR: this environment is genuinely NOT the signer, and enforcement is genuinely off.
    assert _own_principal(store) == ENV_B != ENV_A
    from rebar import config

    cfg = config.compose_config(root=str(store))
    assert cfg.verify.require_environment is None
    assert cfg.verify.opcert_enforce_since is None

    out = signing.verify_signature(tid, kind=KIND, repo_root=str(store))
    assert out["verdict"] == "certified", out["reason"]
    assert out["verified"] is True
    assert out["key_id"] == ENV_A  # certified UNDER A's identity, verified in B
    # The acceptance is VISIBLE, not silent: the trust basis names the unpinned signer key.
    assert out["trust_basis"] == "envelope_key"


def test_cross_environment_cert_unblocks_the_claim_gate(store: Path, tmp_path: Path) -> None:
    """The reported symptom: the claim gate refusing a cert the MCP server signed."""
    from rebar.llm.plan_review.attest_gate import claim_gate_check

    tid = rebar.create_ticket("task", "claim me", repo_root=str(store))
    _sign_as_env_a(store, tmp_path, tid)
    assert _own_principal(store) == ENV_B != ENV_A

    gate = claim_gate_check(tid, repo_root=str(store))
    # ANCHOR: the gate actually RAN (it reached a real verdict, not a store-freshness bail-out)
    # and it is no longer refusing on environment identity.
    assert gate["verdict"] != "foreign_key", gate["reason"]
    assert "foreign_key" not in gate["reason"]
    assert "signed by a different environment" not in gate["reason"]


def test_pinned_environment_key_is_the_trust_basis_when_available(
    store: Path, tmp_path: Path
) -> None:
    """When the signer IS pinned in ``.rebar/trusted_environments.yaml``, that pinned key —
    not the envelope's own — is the trust root, and the result says so."""
    tid = rebar.create_ticket("task", "pinned", repo_root=str(store))
    key_a = _sign_as_env_a(store, tmp_path, tid)
    _pin_environment(store, ENV_A, key_a)
    assert _own_principal(store) == ENV_B != ENV_A

    out = signing.verify_signature(tid, kind=KIND, repo_root=str(store))
    assert out["verdict"] == "certified", out["reason"]
    assert out["trust_basis"] == "pinned_environment"


# ── negative control: identity loosened, signature checking NOT loosened ──────
def test_forged_cross_environment_payload_is_still_refused(store: Path, tmp_path: Path) -> None:
    """The assertion that proves ONLY environment identity was loosened: a cross-environment
    envelope whose signed content was rewritten is still refused, by the signature check."""
    tid = rebar.create_ticket("task", "forged", repo_root=str(store))
    _sign_as_env_a(store, tmp_path, tid)
    _forge_payload(store, tid)
    assert _own_principal(store) == ENV_B != ENV_A

    out = signing.verify_signature(tid, kind=KIND, repo_root=str(store))
    assert out["verified"] is False
    assert out["verdict"] == "mismatch", out["reason"]
    # ANCHOR: it got as far as the envelope-key trust root — i.e. the SCHEME actually ran and
    # rejected it; it was not refused earlier for want of a key.
    assert out["trust_basis"] == "envelope_key"


def test_forged_pinned_environment_payload_is_still_refused(store: Path, tmp_path: Path) -> None:
    """The same control with the signer PINNED: a pinned environment does not make a broken
    signature verify."""
    tid = rebar.create_ticket("task", "forged-pinned", repo_root=str(store))
    key_a = _sign_as_env_a(store, tmp_path, tid)
    _pin_environment(store, ENV_A, key_a)
    _forge_payload(store, tid)

    out = signing.verify_signature(tid, kind=KIND, repo_root=str(store))
    assert out["verified"] is False
    assert out["verdict"] == "mismatch", out["reason"]
    assert out["trust_basis"] == "pinned_environment"


def test_pinned_environment_refuses_a_substituted_key(store: Path, tmp_path: Path) -> None:
    """A cert re-signed with an ATTACKER's key while still claiming environment A is refused
    against A's PINNED key. This is what the pinned trust root buys over the envelope's own key,
    and is the property the deferred trusted-set feature restores everywhere."""
    tid = rebar.create_ticket("task", "substituted", repo_root=str(store))
    key_a = _sign_as_env_a(store, tmp_path, tid)
    _pin_environment(store, ENV_A, key_a)
    _resign_with_attacker_key(store, tmp_path, tid)

    out = signing.verify_signature(tid, kind=KIND, repo_root=str(store))
    assert out["verified"] is False
    assert out["verdict"] == "mismatch", out["reason"]
    assert out["trust_basis"] == "pinned_environment"


def test_envelope_with_no_parsable_key_is_refused_not_waved_through(
    store: Path, tmp_path: Path
) -> None:
    """When NO signer key can be obtained at all — no own key, nothing pinned, and no embedded
    key to read — the result is a refusal naming that, never a silent ``certified``."""
    import json

    from rebar._commands._seam import tracker_dir
    from rebar.attest import dsse

    tid = rebar.create_ticket("task", "keyless", repo_root=str(store))
    _sign_as_env_a(store, tmp_path, tid)
    tdir = Path(layout_ticket_dir(tracker_dir(str(store)), tid))
    path = sorted(tdir.glob("*-SIGNATURE.json"))[-1]
    ev = json.loads(path.read_text(encoding="utf-8"))
    envelope = dsse.decode(ev["data"]["envelope"])
    ev["data"]["envelope"] = dsse.encode(
        envelope.payload_type, envelope.payload, [{"keyid": ENV_A, "sig": b"not-an-sshsig-blob"}]
    )
    path.write_text(json.dumps(ev, ensure_ascii=False), encoding="utf-8")
    for cache in tdir.glob(".cache.json"):
        cache.unlink()

    out = signing.verify_signature(tid, kind=KIND, repo_root=str(store))
    assert out["verified"] is False
    assert out["verdict"] == "foreign_key"
    assert out["trust_basis"] is None
    assert "no public key is obtainable" in out["reason"]


# ── AC2: the opt-in enforcement seam still restricts the signer when SET ──────
def test_enforcement_on_rejects_an_environment_other_than_the_required_one(
    store: Path, tmp_path: Path
) -> None:
    tid = rebar.create_ticket("task", "enforced", repo_root=str(store))
    key_a = _sign_as_env_a(store, tmp_path, tid)
    _pin_environment(store, ENV_A, key_a)
    # A cert from A, but the operator requires a THIRD environment.
    _enforce(store, "some-other-required-environment")

    from rebar import config

    cfg = config.compose_config(root=str(store))
    assert cfg.verify.require_environment == "some-other-required-environment"
    assert cfg.verify.opcert_enforce_since == "HEAD"

    out = signing.verify_signature(tid, kind=KIND, repo_root=str(store))
    assert out["verified"] is False
    assert out["verdict"] == "foreign_key", out["reason"]
    assert "some-other-required-environment" in out["reason"]


def test_enforcement_on_accepts_the_required_environment(store: Path, tmp_path: Path) -> None:
    tid = rebar.create_ticket("task", "enforced-ok", repo_root=str(store))
    key_a = _sign_as_env_a(store, tmp_path, tid)
    _pin_environment(store, ENV_A, key_a)
    _enforce(store, ENV_A)  # A IS the required environment — still not this environment (B)

    assert _own_principal(store) == ENV_B != ENV_A
    out = signing.verify_signature(tid, kind=KIND, repo_root=str(store))
    assert out["verdict"] == "certified", out["reason"]
    assert out["trust_basis"] == "pinned_environment"


def test_enforcement_on_withholds_the_envelope_key_fallback(store: Path, tmp_path: Path) -> None:
    """With enforcement ON, the required environment must be verified against its PINNED key —
    never against a key the certificate supplies about itself. An unpinned required environment
    is therefore refused, not self-certified."""
    tid = rebar.create_ticket("task", "enforced-unpinned", repo_root=str(store))
    _sign_as_env_a(store, tmp_path, tid)
    _enforce(store, ENV_A)  # required environment matches the signer, but nothing is pinned
    assert not (store / ".rebar" / "trusted_environments.yaml").exists()

    out = signing.verify_signature(tid, kind=KIND, repo_root=str(store))
    assert out["verified"] is False
    assert out["verdict"] == "foreign_key", out["reason"]
    assert out["trust_basis"] is None
