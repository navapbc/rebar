"""Worker-level tests for the trusted op-cert gate service (story ee0b).

FastAPI-free: they drive the job core (:func:`rebar.opcert_service.jobs.run_job`) directly with a
FAKE authoritative source (a local rebar store serving as both remotes) and a FAKE SSM fetch (a
real Ed25519 key, no network/AWS/LLM). The LLM dispatch is injected so no billable call is made,
while the SIGNING is real — so the integrity / no-push / key-cleanup properties are exercised
against real behavior.
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
from rebar.opcert_service.keyprov import compose_signer

pytestmark = pytest.mark.unit

_AC = (
    "## Acceptance Criteria\n"
    "- [ ] the widget is built and wired to the CLI\n"
    "- [ ] tests cover the happy path and one edge case\n\n"
    "See src/rebar/widget.py for the implementation surface. This description is long enough to "
    "clear the clarity floor and carries a checklist so the gates would pass."
)


def _run(cwd: str, *args: str) -> str:
    proc = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def _make_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, str, str]:
    """A local rebar store with `main` (code) + `tickets` (state) branches and one ticket.

    Returns ``(source_url, ticket_id, main_head_sha)``. Used as BOTH the review and tickets remote.
    """
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


def _signer(tmp_path: Path):
    """Compose a startup signer from a fresh Ed25519 key (the post-6f14 seam that replaces the
    old per-job ``ssm_fetcher``). Principal is the same ``nava-opcert-test-1`` env id ``_cfg``
    signs under."""
    key = tmp_path / "envkey"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(key), "-N", "", "-q", "-C", "env"],
        check=True,
        capture_output=True,
    )
    key.chmod(0o600)
    kcfg = OpcertServiceConfig(env_id="nava-opcert-test-1", key_path=str(key))
    return compose_signer(kcfg)


def _cfg(source_url: str) -> OpcertServiceConfig:
    return OpcertServiceConfig(
        review_remote_url=source_url,
        tickets_remote_url=source_url,
        review_branch="main",
        guard="secret",
        env_id="nava-opcert-test-1",
        job_timeout_seconds=900.0,
        port=8080,
    )


def _pass_completion(tid, rr):
    return {"verdict": "PASS", "model": "fake-model", "runner": "fake-runner"}


# ─────────────────────────── integrity: server derives bound values ───────────


def test_completion_pass_binds_server_derived_values(tmp_path, monkeypatch):
    src, tid, main_head = _make_source(tmp_path, monkeypatch)
    cfg = _cfg(src)
    fields = jobs.run_job(
        ticket_id=tid,
        kind="completion-verifier",
        cfg=cfg,
        signer=_signer(tmp_path),
        verify_completion_fn=_pass_completion,
    )
    assert fields["status"] == "completed"
    assert fields["verdict"] == "PASS"
    assert fields["envelope"]  # envelope present ONLY on PASS

    # The SIGNED subject binds the SERVER-derived values, not anything a client could send.
    env = dsse.decode(fields["envelope"])
    predicate = json.loads(env.payload)["predicate"]
    assert predicate["merged_log_commit"] == main_head  # = fetched review-remote main tip
    assert predicate["material_fingerprint"]  # server-computed from the fetched ticket state
    assert predicate["ticket_id"] == tid
    assert predicate["kind"] == "completion-verifier"
    # The reported fields mirror the signed payload (never the plaintext mirror / client input).
    assert fields["merged_log_commit"] == predicate["merged_log_commit"]
    assert fields["material_fingerprint"] == predicate["material_fingerprint"]


# ─────────────────────────── the server never pushes ──────────────────────────


def test_server_never_pushes_and_workspace_is_isolated(tmp_path, monkeypatch):
    src, tid, _main = _make_source(tmp_path, monkeypatch)
    cfg = _cfg(src)
    before = _run(src, "rev-parse", "tickets")

    seen: dict = {}

    def gate(tid_, rr):
        from rebar._opcert_binding import current_push_mode

        seen["push"] = current_push_mode()
        seen["remotes"] = subprocess.run(
            ["git", "-C", rr, "remote"], capture_output=True, text=True, check=False
        ).stdout.strip()
        return {"verdict": "PASS", "model": "m", "runner": "r"}

    fields = jobs.run_job(
        ticket_id=tid,
        kind="completion-verifier",
        cfg=cfg,
        signer=_signer(tmp_path),
        verify_completion_fn=gate,
    )
    assert fields["status"] == "completed"
    # The job workspace ran with the context-local push policy = "off" and NO push remote.
    assert seen["push"] == "off"
    assert seen["remotes"] == ""
    # The shared store is byte-identical: the SIGNATURE event landed only in the discarded clone.
    after = _run(src, "rev-parse", "tickets")
    assert after == before


# ─────────────────────────── key materialization + cleanup ────────────────────


def test_key_is_0600_and_gate_sees_no_env_key(tmp_path, monkeypatch):
    """Post-6f14: the composed signer key is 0600 and process-owned, and the gate runs WITHOUT
    ``REBAR_OPCERT_KEY_PATH``/``REBAR_OPCERT_ENV_ID`` leaking into ``os.environ`` (the binding is
    threaded context-locally, not via a process-global env patch)."""
    monkeypatch.delenv("REBAR_OPCERT_KEY_PATH", raising=False)
    monkeypatch.delenv("REBAR_OPCERT_ENV_ID", raising=False)
    src, tid, _main = _make_source(tmp_path, monkeypatch)
    cfg = _cfg(src)
    signer = _signer(tmp_path)
    assert oct(os.stat(signer.key_path).st_mode & 0o777) == "0o600"
    seen: dict = {}

    def gate(tid_, rr):
        # The per-job process env carries NO opcert key/principal — that is the isolation win.
        seen["env_key"] = os.environ.get("REBAR_OPCERT_KEY_PATH")
        seen["env_id"] = os.environ.get("REBAR_OPCERT_ENV_ID")
        return {"verdict": "PASS", "model": "m", "runner": "r"}

    fields = jobs.run_job(
        ticket_id=tid,
        kind="completion-verifier",
        cfg=cfg,
        signer=signer,
        verify_completion_fn=gate,
    )
    assert fields["status"] == "completed"
    assert seen["env_key"] is None
    assert seen["env_id"] is None
    # The signer key is PROCESS-owned (survives the job); cleanup is the owner of removal.
    assert os.path.exists(signer.key_path)
    signer.cleanup()
    assert not os.path.exists(signer.key_path)


def test_signer_key_survives_a_raised_gate_and_cleanup_removes_it(tmp_path, monkeypatch):
    """A gate raising mid-run maps to ``error`` and does NOT tear down the process-owned signer
    key (the signer is composed once at startup, not per job); ``cleanup`` removes it."""
    src, tid, _main = _make_source(tmp_path, monkeypatch)
    cfg = _cfg(src)
    signer = _signer(tmp_path)

    def boom(tid_, rr):
        raise RuntimeError("gate blew up mid-run")

    fields = jobs.run_job(
        ticket_id=tid,
        kind="completion-verifier",
        cfg=cfg,
        signer=signer,
        verify_completion_fn=boom,
    )
    assert fields["status"] == "error"
    assert fields["error"]["class"] == jobs.ERR_INTERNAL
    assert os.path.exists(signer.key_path)  # process-owned, not torn down per job
    signer.cleanup()
    assert not os.path.exists(signer.key_path)


# ─────────────────────────── per-kind verdict mapping ─────────────────────────


def test_completion_fail_completes_with_null_envelope(tmp_path, monkeypatch):
    src, tid, _main = _make_source(tmp_path, monkeypatch)
    cfg = _cfg(src)
    fields = jobs.run_job(
        ticket_id=tid,
        kind="completion-verifier",
        cfg=cfg,
        signer=_signer(tmp_path),
        verify_completion_fn=lambda t, r: {"verdict": "FAIL", "findings": [{"criterion": "x"}]},
    )
    assert fields["status"] == "completed"
    assert fields["verdict"] == "FAIL"
    assert fields["envelope"] is None


@pytest.mark.parametrize(
    ("exc_name", "err_class"),
    [("LLMUnavailableError", jobs.ERR_LLM_UNAVAILABLE), ("LLMError", jobs.ERR_LLM_ERROR)],
)
def test_completion_llm_raise_maps_to_error(tmp_path, monkeypatch, exc_name, err_class):
    src, tid, _main = _make_source(tmp_path, monkeypatch)
    cfg = _cfg(src)
    from rebar.llm import errors as _errors

    exc_cls = getattr(_errors, exc_name)

    def raiser(t, r):
        raise exc_cls("the model is down")

    fields = jobs.run_job(
        ticket_id=tid,
        kind="completion-verifier",
        cfg=cfg,
        signer=_signer(tmp_path),
        verify_completion_fn=raiser,
    )
    assert fields["status"] == "error"
    assert fields["verdict"] is None
    assert fields["envelope"] is None
    assert fields["error"]["class"] == err_class


@pytest.mark.parametrize("verdict", ["BLOCK", "INDETERMINATE"])
def test_plan_review_nonpass_completes_with_null_envelope(tmp_path, monkeypatch, verdict):
    src, tid, _main = _make_source(tmp_path, monkeypatch)
    cfg = _cfg(src)
    fields = jobs.run_job(
        ticket_id=tid,
        kind="plan-review",
        cfg=cfg,
        signer=_signer(tmp_path),
        review_plan_fn=lambda t, r: {"verdict": verdict},
    )
    assert fields["status"] == "completed"
    assert fields["verdict"] == verdict
    assert fields["envelope"] is None


def test_plan_review_pass_signs_internally_and_returns_envelope(tmp_path, monkeypatch):
    src, tid, _main_head = _make_source(tmp_path, monkeypatch)
    cfg = _cfg(src)

    def review_plan_signs(t, rr):
        # Mirror review_plan's sign=True: the gate signs its own attestation internally.
        from rebar import signing

        signing.sign_manifest(
            t,
            ["plan-review: PASS", f"ticket: {t}", "material: deadbeef"],
            kind="plan-review",
            repo_root=rr,
        )
        return {"verdict": "PASS", "model": "m", "runner": "r"}

    fields = jobs.run_job(
        ticket_id=tid,
        kind="plan-review",
        cfg=cfg,
        signer=_signer(tmp_path),
        review_plan_fn=review_plan_signs,
    )
    assert fields["status"] == "completed"
    assert fields["verdict"] == "PASS"
    assert fields["envelope"]
    env = dsse.decode(fields["envelope"])
    predicate = json.loads(env.payload)["predicate"]
    assert predicate["kind"] == "plan-review"
    assert predicate["ticket_id"] == tid


# ─────────────────────────── offline default + config key ─────────────────────


def test_offline_local_sign_and_verify_without_remote_url(tmp_path, monkeypatch):
    """The local op-cert sign/verify path works with verify.opcert_remote_url UNSET and no network
    — the remote path is opt-in and its absence never blocks a local operation."""
    import rebar as _rebar
    from rebar import config, signing

    repo = tmp_path / "local"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, capture_output=True)
    _run(str(repo), "config", "user.email", "l@e.test")
    _run(str(repo), "config", "user.name", "l")
    _run(str(repo), "commit", "-q", "--allow-empty", "-m", "i")
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    _rebar.init_repo(repo_root=str(repo))

    # The new config key defaults to None and is never required locally.
    assert config.load_config(root=str(repo)).verify.opcert_remote_url is None

    tid = _rebar.create_ticket("task", "local task", description=_AC, repo_root=str(repo))
    signing.sign_manifest(
        tid,
        ["completion-verifier: PASS", f"ticket: {tid}", "material: cafe"],
        kind="completion-verifier",
        repo_root=str(repo),
    )
    verdict = signing.verify_signature(tid, kind="completion-verifier", repo_root=str(repo))
    assert verdict["verdict"] == "certified"


def test_config_key_roundtrips_when_set():
    from rebar import config

    overrides = config.parse_cli_overrides(["verify.opcert_remote_url=https://gate.example"])
    with_url = config.load_config(cli_overrides=overrides)
    assert with_url.verify.opcert_remote_url == "https://gate.example"


# ─────────────────────────── Dockerfile contract ──────────────────────────────


def test_dockerfile_declares_openssh_and_uvicorn():
    root = Path(__file__).resolve().parents[2]
    text = (root / "infra" / "compose" / "Dockerfile.opcert").read_text(encoding="utf-8")
    lower = text.lower()
    assert "openssh" in lower
    assert "8.9" in text  # the required OpenSSH floor is declared
    assert "openssh-client" in lower  # the package that provides ssh-keygen
    # The service is started via uvicorn against the ASGI app (exec-form CMD).
    assert "uvicorn" in text
    assert "rebar.opcert_service.app:app" in text
