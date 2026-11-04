"""Held-out integrity and no-push oracles for the trusted op-cert service.

Signed values come from the server's fetched review-remote head and verified envelope payload,
never client input. A passing job leaves the authority byte-identical because its signature event
exists only in the discarded clone. Tests drive the job core with a temporary Ed25519 key and
injected LLM dispatch.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

import rebar
from rebar.attest import dsse
from rebar.opcert_service import jobs
from rebar.opcert_service.config import OpcertServiceConfig
from rebar.opcert_service.keyprov import OpcertSigner, compose_signer

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


@pytest.fixture(scope="module")
def authority(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[str, str, str]]:
    """One authoritative rebar store per module: `main` (code) + `tickets` (state) and one ticket.

    Yields ``(source_url, ticket_id, main_head_sha)``; the URL is both the review and tickets
    remote. Shared because every job only clones it and never pushes back. Building it starts
    about 52 git processes, which per test pushed this module past the per-test budget on a host
    where git is slow to start (bug deformed-cherubical-flycatcher). Teardown fails if any test
    moved a ref."""
    src = tmp_path_factory.mktemp("authoritative")
    subprocess.run(["git", "init", "-q", "-b", "main", str(src)], check=True, capture_output=True)
    _run(str(src), "config", "user.email", "src@e.test")
    _run(str(src), "config", "user.name", "src")
    _run(str(src), "commit", "-q", "--allow-empty", "-m", "genesis")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("REBAR_ROOT", str(src))
        rebar.init_repo(repo_root=str(src))
        tid = rebar.create_ticket("story", "build the widget", description=_AC, repo_root=str(src))
    refs_before = _run(str(src), "show-ref")
    yield str(src), tid, _run(str(src), "rev-parse", "main")
    assert _run(str(src), "show-ref") == refs_before, (
        "a test wrote to the shared authoritative source"
    )


@pytest.fixture
def signer(tmp_path: Path) -> Iterator[OpcertSigner]:
    """A composed signer whose temp-dir key copy is removed at teardown."""
    key = tmp_path / "envkey"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(key), "-N", "", "-q", "-C", "env"],
        check=True,
        capture_output=True,
    )
    key.chmod(0o600)
    composed = compose_signer(OpcertServiceConfig(env_id="nava-opcert-test-1", key_path=str(key)))
    yield composed
    composed.cleanup()


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


def test_signed_cert_binds_server_derived_commit_and_never_pushes(
    authority: tuple[str, str, str], signer: OpcertSigner
) -> None:
    source_url, tid, main_head = authority
    cfg = _cfg(source_url)
    # Snapshot the authoritative source's tickets branch BEFORE the job (no-push precondition).
    tickets_before = _run(source_url, "rev-parse", "tickets")

    result = jobs.run_job(
        ticket_id=tid,
        kind="completion-verifier",
        cfg=cfg,
        signer=signer,
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
