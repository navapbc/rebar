"""RP-04 S6 (6f14) HELD-OUT oracle — op-cert startup key composition.

Withheld from the implementer. Validates the full S6 contract beyond the visible happy path:

AC1  exactly-one-source startup validation (missing / both-set / unreadable / non-Ed25519),
     rejected AT COMPOSITION — before any job runs.
AC2  filesystem: composed key is a process-owned 0700 dir / 0600 file; the source is left
     untouched; cleanup removes only the process copy.
AC4  signer-seam threading through BOTH producer chains — completion AND plan-review sign
     from the composed signer while REBAR_OPCERT_KEY_PATH / REBAR_OPCERT_ENV_ID are UNSET;
     with no binding the developer-local path still signs from the env/genesis default.
AC5  structural: the opcert_service package no longer imports boto3 / SSM / a region; the
     deploy artifacts materialize the key file and drop the per-job SSM param.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

import rebar
from rebar.attest import dsse
from rebar.opcert_service import jobs
from rebar.opcert_service.config import OpcertServiceConfig
from rebar.opcert_service.keyprov import OpcertKeyError, compose_signer

pytestmark = pytest.mark.unit

_AC = (
    "## Acceptance Criteria\n"
    "- [ ] the widget is built and wired to the CLI\n"
    "- [ ] tests cover the happy path and one edge case\n\n"
    "See src/rebar/widget.py for the implementation surface. This description is long enough to "
    "clear the clarity floor and carries a checklist so the gates would pass."
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_OPCERT_PKG = _REPO_ROOT / "src" / "rebar" / "opcert_service"


def _run(cwd: str, *args: str) -> str:
    proc = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def _make_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, str, str]:
    src = tmp_path / "authoritative"
    src.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(src)], check=True, capture_output=True)
    _run(str(src), "config", "user.email", "src@e.test")
    _run(str(src), "config", "user.name", "src")
    _run(str(src), "commit", "-q", "--allow-empty", "-m", "genesis")
    monkeypatch.setenv("REBAR_ROOT", str(src))
    rebar.init_repo(repo_root=str(src))
    tid = rebar.create_ticket("story", "build the widget", description=_AC, repo_root=str(src))
    main_head = _run(str(src), "rev-parse", "main")
    monkeypatch.delenv("REBAR_ROOT", raising=False)
    return str(src), tid, main_head


def _write_ed25519_key(tmp_path: Path, name: str = "deploykey") -> Path:
    key = tmp_path / name
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(key), "-N", "", "-q", "-C", "deploy"],
        check=True,
        capture_output=True,
    )
    key.chmod(0o600)
    return key


def _cfg(source_url: str, *, key_path: str | None = None, private_key: str | None = None):
    return OpcertServiceConfig(
        review_remote_url=source_url,
        tickets_remote_url=source_url,
        review_branch="main",
        guard="secret",
        env_id="nava-opcert-test-1",
        key_path=key_path,
        private_key=private_key,
        job_timeout_seconds=900.0,
        port=8080,
    )


def _pass_completion(tid, rr):
    return {"verdict": "PASS", "model": "fake-model", "runner": "fake-runner"}


def _pass_review_plan(tid, rr):
    # plan-review signs INTERNALLY (review_plan runs with sign=True). Mirror that here by
    # signing through the producer seam — so, running inside run_job's context-local binding,
    # the signature is produced by the BOUND signer (this is exactly what proves AC4 for the
    # plan-review chain: generation/signing.sign_manifest -> _sign_manifest_under_lock).
    from rebar import signing

    signing.sign_manifest(
        tid,
        ["plan-review: PASS", f"ticket: {tid}", "material: deadbeef"],
        kind="plan-review",
        repo_root=rr,
    )
    return {"verdict": "PASS", "model": "fake-model", "runner": "fake-runner"}


def _clear_seam_env(monkeypatch):
    monkeypatch.delenv("REBAR_OPCERT_KEY_PATH", raising=False)
    monkeypatch.delenv("REBAR_OPCERT_ENV_ID", raising=False)
    monkeypatch.delenv("REBAR_OPCERT_PRIVATE_KEY", raising=False)


# ─────────────────────────── AC1: exactly-one-source validation ───────────────


def test_compose_rejects_when_no_source_set(tmp_path, monkeypatch):
    _clear_seam_env(monkeypatch)
    cfg = _cfg(str(tmp_path), key_path=None, private_key=None)
    with pytest.raises(OpcertKeyError):
        compose_signer(cfg)


def test_compose_rejects_when_both_sources_set(tmp_path, monkeypatch):
    _clear_seam_env(monkeypatch)
    key = _write_ed25519_key(tmp_path)
    cfg = _cfg(str(tmp_path), key_path=str(key), private_key=key.read_text(encoding="utf-8"))
    with pytest.raises(OpcertKeyError):
        compose_signer(cfg)


def test_compose_rejects_unreadable_key_path(tmp_path, monkeypatch):
    _clear_seam_env(monkeypatch)
    cfg = _cfg(str(tmp_path), key_path=str(tmp_path / "does-not-exist"))
    with pytest.raises(OpcertKeyError):
        compose_signer(cfg)


def test_compose_rejects_too_permissive_key_path(tmp_path, monkeypatch):
    """AC1 'too-permissive' rejection: a key file with group/other bits (e.g. 0644) is refused
    BEFORE it is copied/used. Teeth: the error must name the permission problem, not merely be
    some OpcertKeyError (a wrong-key or missing-source failure would also raise the base type)."""
    _clear_seam_env(monkeypatch)
    key = _write_ed25519_key(tmp_path, name="loosekey")
    key.chmod(0o644)  # world/group readable — a private signing key must be 0600 or stricter
    cfg = _cfg(str(tmp_path), key_path=str(key))
    with pytest.raises(OpcertKeyError, match="0600 or stricter"):
        compose_signer(cfg)


def test_compose_rejects_non_ed25519_key(tmp_path, monkeypatch):
    _clear_seam_env(monkeypatch)
    key = tmp_path / "rsakey"
    subprocess.run(
        ["ssh-keygen", "-t", "rsa", "-b", "2048", "-f", str(key), "-N", "", "-q", "-C", "rsa"],
        check=True,
        capture_output=True,
    )
    cfg = _cfg(str(tmp_path), key_path=str(key))
    with pytest.raises(OpcertKeyError):
        compose_signer(cfg)


def test_invalid_source_rejected_before_any_job(tmp_path, monkeypatch):
    """A malformed key is caught at composition, so no job is ever attempted with it."""
    _clear_seam_env(monkeypatch)
    bad = tmp_path / "garbage"
    bad.write_text("not a private key\n", encoding="utf-8")
    cfg = _cfg(str(tmp_path), key_path=str(bad))
    with pytest.raises(OpcertKeyError):
        compose_signer(cfg)


# ─────────────────────────── AC2: filesystem ownership + cleanup ──────────────


def test_composed_key_is_process_owned_0600_source_untouched(tmp_path, monkeypatch):
    _clear_seam_env(monkeypatch)
    key = _write_ed25519_key(tmp_path)
    original = key.read_text(encoding="utf-8")
    original_mode = key.stat().st_mode & 0o777
    cfg = _cfg(str(tmp_path), key_path=str(key))

    signer = compose_signer(cfg)
    try:
        copy = Path(signer.key_path)
        assert copy != key  # a process-owned copy, not the source
        assert copy.exists()
        assert (copy.stat().st_mode & 0o777) == 0o600
        assert (Path(signer.runtime_dir).stat().st_mode & 0o777) == 0o700
        # Source is untouched (content + mode).
        assert key.read_text(encoding="utf-8") == original
        assert (key.stat().st_mode & 0o777) == original_mode
    finally:
        signer.cleanup()


def test_cleanup_removes_only_the_copy(tmp_path, monkeypatch):
    _clear_seam_env(monkeypatch)
    key = _write_ed25519_key(tmp_path)
    cfg = _cfg(str(tmp_path), key_path=str(key))
    signer = compose_signer(cfg)
    copy = Path(signer.key_path)
    assert copy.exists()
    signer.cleanup()
    assert not copy.exists()  # the process copy is gone
    assert key.exists()  # the deploy-materialized source remains


# ─────────────────────────── AC4: BOTH producer chains sign from the binding ──


@pytest.mark.parametrize(
    "kind,fn_kw",
    [
        ("completion-verifier", "verify_completion_fn"),
        ("plan-review", "review_plan_fn"),
    ],
)
def test_both_chains_sign_from_composed_signer_without_env(tmp_path, monkeypatch, kind, fn_kw):
    """Completion AND plan-review sign from the composed signer with the seam env UNSET."""
    _clear_seam_env(monkeypatch)
    src, tid, _main = _make_source(tmp_path, monkeypatch)
    key = _write_ed25519_key(tmp_path)
    cfg = _cfg(src, key_path=str(key))
    signer = compose_signer(cfg)
    kw = {fn_kw: _pass_completion if kind == "completion-verifier" else _pass_review_plan}
    try:
        # Prove the binding — not the env — is the key source. Bind membership to a bool
        # first: a bare `... not in os.environ` renders the WHOLE environment on failure.
        seam_env_present = "REBAR_OPCERT_KEY_PATH" in os.environ
        assert not seam_env_present
        fields = jobs.run_job(ticket_id=tid, kind=kind, cfg=cfg, signer=signer, **kw)
    finally:
        signer.cleanup()

    assert fields["status"] == "completed", fields
    assert fields["verdict"] == "PASS"
    assert fields["envelope"]
    env = dsse.decode(fields["envelope"])
    predicate = json.loads(env.payload)["predicate"]
    assert predicate["ticket_id"] == tid
    assert predicate["kind"] == kind
    # TEETH: the envelope is signed under the COMPOSED signer's principal (cfg.env_id), proving
    # the binding was threaded into the signing seam — NOT the ephemeral clone's own genesis key
    # (which would carry a random minted env-id). A broken binding would fail this assertion.
    assert env.signatures[0].keyid == "nava-opcert-test-1"


def test_no_binding_developer_local_signs_from_env(tmp_path, monkeypatch):
    """With no service binding, the developer-local completion signer uses the env/genesis key
    — the composition change must NOT break the un-bound (CLI close) producer path."""
    src, tid, _ = _make_source(tmp_path, monkeypatch)
    key = _write_ed25519_key(tmp_path, name="envkey")
    monkeypatch.setenv("REBAR_OPCERT_KEY_PATH", str(key))
    monkeypatch.setenv("REBAR_OPCERT_ENV_ID", "nava-env-local")

    from rebar._commands.transition_close import sign_completion_verdict

    result = {"verdict": "PASS", "model": "m", "runner": "r"}
    # No signer= argument: the env-provisioned key must satisfy signing.
    sign_completion_verdict(result, tid, src)
    state = rebar.show_ticket(tid, repo_root=src)
    record = (state.get("attestations") or {}).get("completion-verifier")
    assert isinstance(record, dict) and record.get("envelope")


# ─────────────────────────── AC5: no boto3/SSM/region in opcert_service ───────


def test_opcert_service_source_has_no_boto3_or_ssm(tmp_path):
    """AC5: the opcert application runtime carries no AWS/SSM coupling. Targets actual imports
    and runtime identifiers — migration-context docstrings that merely mention SSM are fine."""
    banned = (
        "import boto3",
        "boto3.",
        "WithDecryption",
        "get_parameter(",
        "boto3_ssm_fetcher",
        "ssm_key_param",
        "REBAR_OPCERT_SSM",
        "region_name",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
    )
    offenders = []
    for py in _OPCERT_PKG.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        for needle in banned:
            if needle in text:
                offenders.append(f"{py.name}: {needle}")
    assert not offenders, f"opcert_service must be AWS-free after cutover: {offenders}"


def test_deploy_materializes_key_file_and_drops_ssm_param():
    """fetch-secrets materializes the opcert key to a file; compose binds it; no SSM param."""
    fetch = _REPO_ROOT / "infra" / "scripts" / "fetch-secrets.sh"
    compose = _REPO_ROOT / "infra" / "compose" / "docker-compose.yml"
    text_fetch = fetch.read_text(encoding="utf-8") if fetch.exists() else ""
    text_compose = compose.read_text(encoding="utf-8") if compose.exists() else ""
    combined = text_fetch + text_compose
    assert "REBAR_OPCERT_KEY_PATH" in combined, "deploy must export REBAR_OPCERT_KEY_PATH"
    assert "REBAR_OPCERT_SSM_KEY_PARAM" not in text_compose, (
        "the opcert service must no longer receive a per-job SSM key param"
    )
