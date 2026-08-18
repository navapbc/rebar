"""RP-04 S6 (6f14) VISIBLE happy-path oracle — op-cert startup key composition.

This is the ONLY S6 test the implementer sees. It pins the new startup-composition
seam's happy path: the service composes ONE immutable signer at startup from a
deployment-materialized key FILE (``REBAR_OPCERT_KEY_PATH`` source), and a completion
job signs from that composed signer — with NO per-job SSM fetch and NO reliance on the
signing seam re-reading ``REBAR_OPCERT_KEY_PATH`` / ``REBAR_OPCERT_ENV_ID`` from the
process environment.

New public API this pins (see the ticket plan):
    rebar.opcert_service.keyprov.compose_signer(cfg) -> OpcertSigner
        - reads EXACTLY ONE of cfg.key_path / cfg.private_key
        - validates + copies the key into a process-owned 0700 dir / 0600 file
        - returns a frozen OpcertSigner(key_path, principal, runtime_dir) with .cleanup()
    rebar.opcert_service.jobs.run_job(*, ticket_id, kind, cfg, signer, ...)
        - takes the composed signer (not a per-job ssm_fetcher) and threads it into
          the signing producer so signing uses the composed key.
"""

from __future__ import annotations

import json
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


def _cfg(source_url: str, *, key_path: str) -> OpcertServiceConfig:
    return OpcertServiceConfig(
        review_remote_url=source_url,
        tickets_remote_url=source_url,
        review_branch="main",
        guard="secret",
        env_id="nava-opcert-test-1",
        key_path=key_path,
        job_timeout_seconds=900.0,
        port=8080,
    )


def _pass_completion(tid, rr):
    return {"verdict": "PASS", "model": "fake-model", "runner": "fake-runner"}


def test_compose_signer_then_completion_job_signs(tmp_path, monkeypatch):
    """The startup-composed signer is threaded into the completion signing path."""
    # The signing seam must NOT depend on these being present in the env.
    monkeypatch.delenv("REBAR_OPCERT_KEY_PATH", raising=False)
    monkeypatch.delenv("REBAR_OPCERT_ENV_ID", raising=False)

    src, tid, main_head = _make_source(tmp_path, monkeypatch)
    key = _write_ed25519_key(tmp_path)
    cfg = _cfg(src, key_path=str(key))

    signer = compose_signer(cfg)
    try:
        fields = jobs.run_job(
            ticket_id=tid,
            kind="completion-verifier",
            cfg=cfg,
            signer=signer,
            verify_completion_fn=_pass_completion,
        )
    finally:
        signer.cleanup()

    assert fields["status"] == "completed"
    assert fields["verdict"] == "PASS"
    assert fields["envelope"]  # envelope present ONLY on a signed PASS

    env = dsse.decode(fields["envelope"])
    predicate = json.loads(env.payload)["predicate"]
    assert predicate["merged_log_commit"] == main_head
    assert predicate["ticket_id"] == tid
    assert predicate["kind"] == "completion-verifier"
