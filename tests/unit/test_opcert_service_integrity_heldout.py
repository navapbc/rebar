"""Independent held-out integrity + no-push oracle for the trusted op-cert gate service (ee0b).

NOT shown to the implementer. Two load-bearing properties of the trusted server:
  1. INTEGRITY: the values the signed cert binds are SERVER-DERIVED (the fetched review-remote main
     HEAD, read back from the SIGNED envelope payload) — a client cannot supply them.
  2. NO-PUSH: the authoritative source the server fetched FROM is byte-identical after a completed
     PASS job (the SIGNATURE event lands only in the discarded ephemeral clone).

FastAPI-free: drives the real job core with a fake authoritative source + fake SSM key (a real
Ed25519 key). LLM dispatch is injected (no billable call); the SIGNING and the fetch/clone/discard
are real.
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

pytestmark = pytest.mark.unit

_AC = (
    "## Acceptance Criteria\n"
    "- [ ] the widget is built and wired to the CLI\n"
    "- [ ] tests cover the happy path and one edge case\n\n"
    "Long enough to clear the clarity floor with a checklist so the gates pass. "
    "See src/rebar/widget.py for the implementation surface."
)


def _run(cwd: str, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", cwd, *args], capture_output=True, text=True, check=True
    ).stdout.strip()


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


def _key_fetcher(tmp_path: Path):
    key = tmp_path / "envkey"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(key), "-N", "", "-q", "-C", "env"],
        check=True,
        capture_output=True,
    )
    priv = key.read_text(encoding="utf-8")
    return lambda _param: priv


def _cfg(source_url: str) -> OpcertServiceConfig:
    return OpcertServiceConfig(
        review_remote_url=source_url,
        tickets_remote_url=source_url,
        review_branch="main",
        guard="secret",
        env_id="nava-opcert-test-1",
        ssm_key_param="/rebar/prod/opcert-ed25519-key",
        job_timeout_seconds=900.0,
        port=8080,
    )


def test_signed_cert_binds_server_derived_commit_and_never_pushes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_url, tid, main_head = _make_source(tmp_path, monkeypatch)
    cfg = _cfg(source_url)
    # Snapshot the authoritative source's tickets branch BEFORE the job (no-push precondition).
    tickets_before = _run(source_url, "rev-parse", "tickets")

    result = jobs.run_job(
        ticket_id=tid,
        kind="completion-verifier",
        cfg=cfg,
        ssm_fetcher=_key_fetcher(tmp_path),
        verify_completion_fn=lambda _t, _r: {"verdict": "PASS", "model": "fake", "runner": "fake"},
    )

    assert result["status"] == "completed", result
    assert result["verdict"] == "PASS"
    assert result["envelope"], "a PASS must carry a signed envelope"

    # INTEGRITY: the bound merged_log_commit is the SERVER-fetched review-remote main HEAD, taken
    # from the SIGNED envelope payload — not any client-supplied value.
    payload = json.loads(dsse.decode(result["envelope"]).payload)
    assert payload["predicate"]["merged_log_commit"] == main_head
    assert result["merged_log_commit"] == main_head

    # NO-PUSH: the authoritative source is byte-identical after the completed PASS — the server
    # signed only into its discarded ephemeral clone and never wrote back to the shared branch.
    tickets_after = _run(source_url, "rev-parse", "tickets")
    assert tickets_after == tickets_before, "server must NOT push the SIGNATURE event upstream"
