"""Per-environment op-cert trust list ``verify.trusted_opcert_keys`` (ADR 0125).

A verifying environment may list the (environment, key) pairs it trusts. With the list set, a
valid, correctly bound certificate from a listed pair passes with trust basis ``trust_list``; a
valid certificate from an unlisted pair is ``untrusted``; a broken or rebound certificate still
fails as ``mismatch`` first. Every op-cert result names its signing key (``signing_key``).
Epic nonblack-brigandish-guillemot, story unequaled-sincere-firefly.
"""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
from pathlib import Path

import pytest

import rebar
from rebar import config, signing
from rebar._config_coercion import ConfigError
from rebar._opcert_binding import bound_signer
from rebar.attest import sshsig

try:
    sshsig.ensure_available()
    _SSH_OK = True
except Exception:  # noqa: BLE001
    _SSH_OK = False

pytestmark = pytest.mark.skipif(not _SSH_OK, reason="ssh-keygen >= 8.9 required for SSHSIG")

KIND = "plan-review"
ENV_A = "9f1c8e42-7a3b-4d5e-b6c1-2f0a9d8e7c65"  # the signing environment
ENV_B = "b8a354e0-1c2d-4e3f-9a8b-7c6d5e4f3a2b"  # the verifying environment
ENV_C = "ade0f2ef-51cc-45a2-bb97-c09846cd3df5"  # another listed environment


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
    monkeypatch.delenv("REBAR_VERIFY_TRUSTED_OPCERT_KEYS", raising=False)
    monkeypatch.setenv("REBAR_OPCERT_ENV_ID", ENV_B)
    rebar.init_repo(repo_root=str(repo))
    return repo


def _keypair(tmp_path: Path, name: str) -> Path:
    d = tmp_path / "secrets"
    d.mkdir(exist_ok=True)
    key = d / name
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(key), "-N", "", "-q", "-C", name],
        check=True,
        capture_output=True,
    )
    key.chmod(0o600)
    return key


def _pub(key: Path) -> str:
    return Path(str(key) + ".pub").read_text(encoding="utf-8").strip()


def _fingerprint(key: Path) -> str:
    blob = base64.b64decode(_pub(key).split()[1])
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii").rstrip("=")


class _Signer:
    def __init__(self, key_path: Path, principal: str) -> None:
        self._key_path = str(key_path)
        self._principal = principal

    @property
    def key_path(self) -> str:
        return self._key_path

    @property
    def principal(self) -> str | None:
        return self._principal


def _sign(store: Path, key: Path, principal: str, title: str = "t") -> str:
    tid = rebar.create_ticket("task", title, repo_root=str(store))
    with bound_signer(_Signer(key, principal)):
        rec = signing.sign_manifest(tid, [f"{KIND}: PASS"], kind=KIND, repo_root=str(store))
    assert rec["principal"] == principal
    return tid


def _trust(store: Path, *entries: str, require_env: str | None = None) -> None:
    lines = ["[verify]"]
    lines.append("trusted_opcert_keys = " + json.dumps(list(entries)))
    if require_env is not None:
        lines.append(f'require_environment = "{require_env}"')
        lines.append('opcert_enforce_since = "HEAD"')
    (store / "rebar.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _last_sig_path(store: Path, tid: str) -> Path:
    from rebar._commands._seam import tracker_dir

    tdir = Path(tracker_dir(str(store))) / tid
    return sorted(tdir.glob("*-SIGNATURE.json"))[-1]


def _drop_cache(path: Path) -> None:
    for cache in path.parent.glob(".cache.json"):
        cache.unlink()


def _forge_payload(store: Path, tid: str) -> None:
    path = _last_sig_path(store, tid)
    ev = json.loads(path.read_text(encoding="utf-8"))
    env = json.loads(ev["data"]["envelope"])
    payload = json.loads(base64.b64decode(env["payload"]))
    payload["predicate"]["manifest"] = [f"{KIND}: PASS", "forged: an extra attested step"]
    env["payload"] = base64.b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).decode("ascii")
    ev["data"]["envelope"] = json.dumps(env)
    path.write_text(json.dumps(ev, ensure_ascii=False), encoding="utf-8")
    _drop_cache(path)


def _replay_onto(store: Path, src_tid: str, dst_tid: str) -> None:
    """Copy src's valid SIGNATURE event onto dst — a cross-ticket replay."""
    src = _last_sig_path(store, src_tid)
    from rebar._commands._seam import tracker_dir

    dst_dir = Path(tracker_dir(str(store))) / dst_tid
    ev = json.loads(src.read_text(encoding="utf-8"))
    ev["ticket_id"] = dst_tid
    (dst_dir / src.name).write_text(json.dumps(ev, ensure_ascii=False), encoding="utf-8")
    _drop_cache(dst_dir / src.name)


def _verify(store: Path, tid: str) -> dict:
    return signing.verify_signature(tid, kind=KIND, repo_root=str(store))


# ── list set ────────────────────────────────────────────────────────────────────
def test_listed_key_for_its_environment_passes_with_trust_list_basis(
    store: Path, tmp_path: Path
) -> None:
    key_a = _keypair(tmp_path, "a")
    tid = _sign(store, key_a, ENV_A)
    _trust(store, f"{ENV_A} {_pub(key_a)}")

    out = _verify(store, tid)
    assert out["verdict"] == "certified", out["reason"]
    assert out["verified"] is True
    assert out["trust_basis"] == "trust_list"
    assert out["signing_key"] == _fingerprint(key_a)
    assert out["key_id"] == ENV_A


def test_unlisted_key_is_untrusted(store: Path, tmp_path: Path) -> None:
    key_a = _keypair(tmp_path, "a")
    other = _keypair(tmp_path, "other")
    tid = _sign(store, key_a, ENV_A)
    _trust(store, f"{ENV_A} {_pub(other)}")

    out = _verify(store, tid)
    assert out["verified"] is False
    assert out["verdict"] == "untrusted", out["reason"]
    assert _fingerprint(key_a) in out["reason"]
    assert ENV_A in out["reason"]
    assert out["signing_key"] == _fingerprint(key_a)


def test_listed_key_under_another_environment_is_untrusted(store: Path, tmp_path: Path) -> None:
    key_a = _keypair(tmp_path, "a")
    tid = _sign(store, key_a, ENV_A)
    _trust(store, f"{ENV_C} {_pub(key_a)}")  # key listed, but for a different environment

    out = _verify(store, tid)
    assert out["verified"] is False
    assert out["verdict"] == "untrusted", out["reason"]


def test_tampered_payload_is_mismatch_not_untrusted(store: Path, tmp_path: Path) -> None:
    key_a = _keypair(tmp_path, "a")
    tid = _sign(store, key_a, ENV_A)
    _trust(store, f"{ENV_A} {_pub(key_a)}")
    _forge_payload(store, tid)

    out = _verify(store, tid)
    assert out["verified"] is False
    assert out["verdict"] == "mismatch", out["reason"]


def test_cross_ticket_replay_is_mismatch_not_untrusted(store: Path, tmp_path: Path) -> None:
    key_a = _keypair(tmp_path, "a")
    src = _sign(store, key_a, ENV_A, "src")
    dst = rebar.create_ticket("task", "dst", repo_root=str(store))
    _trust(store, f"{ENV_A} {_pub(key_a)}")
    _replay_onto(store, src, dst)

    out = _verify(store, dst)
    assert out["verified"] is False
    assert out["verdict"] == "mismatch", out["reason"]


def test_require_environment_naming_another_environment_is_foreign_key(
    store: Path, tmp_path: Path
) -> None:
    key_a = _keypair(tmp_path, "a")
    tid = _sign(store, key_a, ENV_A)
    _trust(store, f"{ENV_A} {_pub(key_a)}", require_env=ENV_C)

    out = _verify(store, tid)
    assert out["verified"] is False
    assert out["verdict"] == "foreign_key", out["reason"]


def test_list_unset_leaves_verification_unchanged(store: Path, tmp_path: Path) -> None:
    """With no list, a cross-environment certificate verifies on the pre-existing basis."""
    key_a = _keypair(tmp_path, "a")
    tid = _sign(store, key_a, ENV_A)
    assert config.compose_config(root=str(store)).verify.trusted_opcert_keys == ()

    out = _verify(store, tid)
    assert out["verdict"] == "certified", out["reason"]
    assert out["trust_basis"] != "trust_list"
    assert out["signing_key"] == _fingerprint(key_a)


# ── config parsing ──────────────────────────────────────────────────────────────
def test_comma_separated_env_value_parses_each_entry(
    store: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keys = [_pub(_keypair(tmp_path, n)) for n in ("k1", "k2", "k3")]
    value = ", ".join(f"{env} {k}" for env, k in zip((ENV_A, ENV_A, ENV_C), keys, strict=True))
    monkeypatch.setenv("REBAR_VERIFY_TRUSTED_OPCERT_KEYS", value)

    got = config.compose_config(root=str(store)).verify.trusted_opcert_keys
    assert len(got) == 3
    assert got[2].startswith(ENV_C + " ssh-ed25519 ")


@pytest.mark.parametrize(
    "entry",
    [
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIB6u+ddQiB4ck/ToORQkQwtehODF4VqLirMIUNFpobZZ",
        f"{ENV_A} ssh-ed25519",
        f"{ENV_A} ssh-ed25519 not*base64!",
    ],
)
def test_malformed_entry_raises_naming_its_position(store: Path, entry: str) -> None:
    _trust(store, entry)
    with pytest.raises(ConfigError) as exc:
        config.compose_config(root=str(store))
    assert "verify.trusted_opcert_keys" in str(exc.value)
    assert "entry 1" in str(exc.value)


def test_empty_env_override_raises(store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REBAR_VERIFY_TRUSTED_OPCERT_KEYS", "")
    with pytest.raises(ConfigError):
        config.compose_config(root=str(store))


# ── MCP /health op-cert signer status ───────────────────────────────────────────
def test_health_listed_signer_is_expected_and_matched(store: Path, tmp_path: Path) -> None:
    from rebar._mcp_opcert_health import opcert_signer_degraded, opcert_signing_status

    key_a = _keypair(tmp_path, "a")
    _trust(store, f"{ENV_A} {_pub(key_a)}")
    status = opcert_signing_status(_Signer(key_a, ENV_A), str(store))
    assert status["expected"] is True
    assert status["matched"] is True
    assert "error" not in status
    assert opcert_signer_degraded(status) is False


def test_health_unlisted_signer_is_degraded(store: Path, tmp_path: Path) -> None:
    from rebar._mcp_opcert_health import opcert_signer_degraded, opcert_signing_status

    key_a = _keypair(tmp_path, "a")
    other = _keypair(tmp_path, "other")
    _trust(store, f"{ENV_A} {_pub(other)}")
    status = opcert_signing_status(_Signer(key_a, ENV_A), str(store))
    assert status["expected"] is True
    assert status["matched"] is False
    assert opcert_signer_degraded(status) is True
