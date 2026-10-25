"""A rotated own-principal signing key must not invalidate its earlier op-certs (d909).

When this environment rotates its op-cert key, certificates signed under the previous key
carry the same principal but a key that is no longer the current own key. The verifier used
only the current own key for the own principal, so those certificates failed as ``mismatch``.
The previous key, still pinned and unrevoked in ``.rebar/trusted_environments.yaml``, must be
trusted too; an unpinned or revoked key, or a changed payload, must still fail.
"""

from __future__ import annotations

import base64
import json
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
OWN_ENV = "b8a354e0-1c2d-4e3f-9a8b-7c6d5e4f3a2b"
_SCHEMA = Path(__file__).resolve().parents[2] / "src/rebar/schemas/plan_review_status.schema.json"


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
    monkeypatch.setenv("REBAR_OPCERT_ENV_ID", OWN_ENV)
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
    def __init__(self, key_path: Path, principal: str) -> None:
        self._key_path, self._principal = str(key_path), principal

    @property
    def key_path(self) -> str:
        return self._key_path

    @property
    def principal(self) -> str | None:
        return self._principal


def _sign_as_own_principal(store: Path, key: Path, tid: str) -> None:
    """Mint a plan-review op-cert under ``key`` carrying THIS environment's own principal."""
    with bound_signer(_Signer(key, OWN_ENV)):
        record = signing.sign_manifest(tid, [f"{KIND}: PASS"], kind=KIND, repo_root=str(store))
    assert record["principal"] == OWN_ENV, "the cert must carry the own principal"


def _pin(store: Path, key: Path, *, revoked: bool = False) -> None:
    """Pin ``key`` for the own principal; ``revoked`` marks it revoked at a log position."""
    pub = Path(str(key) + ".pub").read_text(encoding="utf-8").strip()
    revoked_at = "'9-00000000-0000-4000-8000-000000000000'" if revoked else "null"
    d = store / ".rebar"
    d.mkdir(exist_ok=True)
    (d / "trusted_environments.yaml").write_text(
        "environments:\n"
        f"  - env_id: {OWN_ENV!r}\n"
        "    keys:\n"
        f"      - public_key: {pub!r}\n"
        "        added_at_log_position: '0-00000000-0000-4000-8000-000000000000'\n"
        f"        revoked_at_log_position: {revoked_at}\n",
        encoding="utf-8",
    )


def _hold_current_own_key(store: Path) -> None:
    """Give this clone a current own key, i.e. the post-rotation state."""
    from rebar._commands._seam import tracker_dir

    signing.ensure_opcert_key(str(tracker_dir(str(store))))


def _own_pubs(store: Path) -> list[str]:
    from rebar._commands._seam import tracker_dir
    from rebar._opcert_signing import _opcert_own_public_keys

    return _opcert_own_public_keys(str(tracker_dir(str(store))))


def _forge_payload(store: Path, tid: str) -> None:
    from rebar._commands._seam import tracker_dir

    tdir = Path(tracker_dir(str(store))) / tid
    path = sorted(tdir.glob("*-SIGNATURE.json"))[-1]
    ev = json.loads(path.read_text(encoding="utf-8"))
    env = json.loads(ev["data"]["envelope"])
    payload = json.loads(base64.b64decode(env["payload"]))
    payload["predicate"]["manifest"] = [f"{KIND}: PASS", "forged: an extra attested step"]
    env["payload"] = base64.b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).decode("ascii")
    ev["data"]["envelope"] = json.dumps(env)
    path.write_text(json.dumps(ev, ensure_ascii=False), encoding="utf-8")
    for cache in tdir.glob(".cache.json"):
        cache.unlink()


def _verify(store: Path, tid: str) -> dict:
    return signing.verify_signature(tid, kind=KIND, repo_root=str(store))


# ── the reported defect ───────────────────────────────────────────────────────
def test_cert_under_a_pinned_unrevoked_previous_own_key_verifies_after_rotation(
    store: Path, tmp_path: Path
) -> None:
    old_key = _keypair(tmp_path, "old")
    tid = rebar.create_ticket("task", "rotated", repo_root=str(store))
    _sign_as_own_principal(store, old_key, tid)
    _hold_current_own_key(store)  # the rotation: a new, different own key
    _pin(store, old_key)

    old_pub = Path(str(old_key) + ".pub").read_text(encoding="utf-8").split()[1]
    assert old_pub not in " ".join(_own_pubs(store)), "ANCHOR: the old key is no longer the own key"

    out = _verify(store, tid)
    assert out["verdict"] == "certified", out["reason"]
    assert out["trust_basis"] == "pinned_environment"


def test_cert_under_the_current_own_key_still_reports_own_key(store: Path, tmp_path: Path) -> None:
    """Control: the common case is untouched and still attributed to the own key."""
    _hold_current_own_key(store)
    from rebar._commands._seam import tracker_dir
    from rebar._opcert_signing import opcert_key_path

    tid = rebar.create_ticket("task", "current", repo_root=str(store))
    _sign_as_own_principal(store, Path(opcert_key_path(str(tracker_dir(str(store))))), tid)

    out = _verify(store, tid)
    assert out["verdict"] == "certified", out["reason"]
    assert out["trust_basis"] == "own_key"


# ── what must still fail ──────────────────────────────────────────────────────
def test_cert_under_an_unpinned_previous_key_still_fails(store: Path, tmp_path: Path) -> None:
    old_key = _keypair(tmp_path, "old")
    tid = rebar.create_ticket("task", "unpinned", repo_root=str(store))
    _sign_as_own_principal(store, old_key, tid)
    _hold_current_own_key(store)  # no pin file at all

    out = _verify(store, tid)
    assert out["verified"] is False
    assert out["verdict"] == "mismatch", out["reason"]


def test_cert_under_a_pinned_but_revoked_previous_key_still_fails(
    store: Path, tmp_path: Path
) -> None:
    old_key = _keypair(tmp_path, "old")
    tid = rebar.create_ticket("task", "revoked", repo_root=str(store))
    _sign_as_own_principal(store, old_key, tid)
    _hold_current_own_key(store)
    _pin(store, old_key, revoked=True)

    out = _verify(store, tid)
    assert out["verified"] is False
    assert out["verdict"] == "mismatch", out["reason"]


def test_a_forged_payload_under_a_pinned_previous_key_still_fails(
    store: Path, tmp_path: Path
) -> None:
    old_key = _keypair(tmp_path, "old")
    tid = rebar.create_ticket("task", "forged", repo_root=str(store))
    _sign_as_own_principal(store, old_key, tid)
    _hold_current_own_key(store)
    _pin(store, old_key)
    _forge_payload(store, tid)

    out = _verify(store, tid)
    assert out["verified"] is False
    assert out["verdict"] == "mismatch", out["reason"]


# ── the fall-through path: own principal, but no own key held in this clone ───
def test_no_local_key_trusts_a_pinned_unrevoked_key_for_the_own_principal(
    store: Path, tmp_path: Path
) -> None:
    old_key = _keypair(tmp_path, "old")
    tid = rebar.create_ticket("task", "no-own-key", repo_root=str(store))
    _sign_as_own_principal(store, old_key, tid)
    _pin(store, old_key)
    assert _own_pubs(store) == [], "ANCHOR: this clone holds no own key"

    out = _verify(store, tid)
    assert out["verdict"] == "certified", out["reason"]
    assert out["trust_basis"] == "pinned_environment"


def _enforce_own_environment(store: Path) -> None:
    """Flip the opt-in enforcement pair on: only the required environment's keys are trusted."""
    (store / "rebar.toml").write_text(
        f'[verify]\nrequire_environment = "{OWN_ENV}"\nopcert_enforce_since = "HEAD"\n',
        encoding="utf-8",
    )


def test_no_local_key_refuses_a_revoked_pinned_key_when_enforcement_is_on(
    store: Path, tmp_path: Path
) -> None:
    """Revocation must bite where trust is restricted to pinned keys."""
    old_key = _keypair(tmp_path, "old")
    tid = rebar.create_ticket("task", "no-own-key-revoked-enforced", repo_root=str(store))
    _sign_as_own_principal(store, old_key, tid)
    _pin(store, old_key, revoked=True)
    _enforce_own_environment(store)
    assert _own_pubs(store) == [], "ANCHOR: this clone holds no own key"

    out = _verify(store, tid)
    assert out["verified"] is False, "a revoked key must not certify under enforcement"
    assert out["verdict"] == "foreign_key", out["reason"]


def test_no_local_key_does_not_label_a_revoked_key_as_pinned_trust(
    store: Path, tmp_path: Path
) -> None:
    """In advisory mode an unpinned key still verifies on its embedded key (the c21f design),
    but a revoked key must not be reported as pinned trust."""
    old_key = _keypair(tmp_path, "old")
    tid = rebar.create_ticket("task", "no-own-key-revoked-advisory", repo_root=str(store))
    _sign_as_own_principal(store, old_key, tid)
    _pin(store, old_key, revoked=True)
    assert _own_pubs(store) == [], "ANCHOR: this clone holds no own key"

    out = _verify(store, tid)
    assert out["trust_basis"] == "envelope_key", out
    assert out["trust_basis"] != "pinned_environment"


# ── plan_review_status must be able to report a signature failure ─────────────
_REACHABLE_SIGNATURE_VERDICTS = (
    "mismatch",
    "invalid",
    "foreign_key",
    "unavailable",
    "unknown_kind",
    "unknown_scheme",
)
_UNREACHABLE_HERE = (
    "unknown_principal",
    "subject_mismatch",
    "malformed_statement",
    "key_not_valid_at_era",
)


def _validate(result: dict) -> None:
    import jsonschema

    jsonschema.validate(result, json.loads(_SCHEMA.read_text(encoding="utf-8")))


def test_a_real_signature_mismatch_validates_against_the_status_schema(
    store: Path, tmp_path: Path
) -> None:
    """The reported adjacent defect: MCP plan_review_status failed output validation."""
    import rebar.llm

    old_key = _keypair(tmp_path, "old")
    tid = rebar.create_ticket("task", "status", repo_root=str(store))
    _sign_as_own_principal(store, old_key, tid)
    _hold_current_own_key(store)  # old key unpinned: the status read sees a mismatch

    status = dict(rebar.llm.plan_review_status(tid, repo_root=str(store)))
    assert status["verdict"] == "mismatch", status
    _validate(status)


@pytest.mark.parametrize("verdict", _REACHABLE_SIGNATURE_VERDICTS)
def test_every_reachable_signature_verdict_is_declared(verdict: str) -> None:
    _validate(
        {
            "ok": False,
            "verdict": verdict,
            "reason": "x",
            "verified_at_sha": None,
            "signed_at": None,
        }
    )


def test_verdicts_the_status_path_cannot_produce_stay_out_of_the_enum() -> None:
    declared = json.loads(_SCHEMA.read_text(encoding="utf-8"))["properties"]["verdict"]["enum"]
    assert not set(_UNREACHABLE_HERE) & set(declared), (
        "authorship-only and merge-gate-only verdicts are unreachable through plan_review_status"
    )
