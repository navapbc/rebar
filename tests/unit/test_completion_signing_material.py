"""A completion PASS is bound to the ticket material the verifier READ (bug 0521-7e68).

Before the fix the default (materialized) attested path recorded no material of its own, and
signing fell back to a live read of the ticket. A description edited during the run was
therefore signed as verified, and a close reused that signature.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
from rebar import signing
from rebar._commands import close_autoresume, transition_close
from rebar._snapshot.repo_snapshot import SnapshotHandle, materialize_tickets
from rebar.llm import completion
from rebar.llm.plan_review.attest import current_material_fingerprint

pytestmark = pytest.mark.unit


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "t@e.com"),
        ("git", "config", "user.name", "t"),
        ("git", "commit", "-q", "--allow-empty", "-m", "i"),
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    monkeypatch.setenv("REBAR_GATE_TMPDIR", str(tmp_path / "gate"))
    monkeypatch.chdir(repo)
    rebar.init_repo(repo_root=str(repo))
    return repo


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _in_progress_ticket() -> str:
    tid = rebar.create_ticket("task", "read-time material", description="## Plan\nD1\n")
    rebar.claim(tid, assignee="t@e.com")
    return tid


def _pass(sha: str, material: str | None) -> dict:
    result = {
        "verdict": "PASS",
        "model": "m",
        "runner": "r",
        "source": "attested",
        "certifiable": True,
        "verified_at_sha": sha,
    }
    if material is not None:
        result["material_fingerprint"] = material
    return result


def _signature_count(repo: Path, tid: str) -> int:
    return len(list((repo / ".tickets-tracker" / tid).glob("*-SIGNATURE.json")))


def test_attested_run_records_the_material_the_verifier_read(store, monkeypatch):
    tid = _in_progress_ticket()
    read_time = current_material_fingerprint(tid)
    snapshot = materialize_tickets(repo_root=str(store), fetch=False)
    rebar.edit_ticket(tid, description="## Plan\nD2 edited during the run\n")
    assert current_material_fingerprint(tid) != read_time

    monkeypatch.setattr(completion, "_verify_completion_inner", lambda *a, **k: {"verdict": "PASS"})
    handle = SnapshotHandle(path=store, sha=_head(store), source="attested", tickets_path=snapshot)
    result = completion._run_completion_at_handle(
        tid,
        graph=None,
        repo_root=str(store),
        config=None,
        runner=None,
        phase_metrics=None,
        handle=handle,
        ticket_view=None,
        ticket_read_mode=None,
    )

    assert result["material_fingerprint"] == read_time


def test_signing_refuses_when_the_ticket_moved_after_the_read(store):
    tid = _in_progress_ticket()
    read_time = current_material_fingerprint(tid)
    rebar.edit_ticket(tid, description="## Plan\nD2 edited during the run\n")

    with pytest.raises(signing.SigningError, match="material changed"):
        transition_close.sign_completion_verdict(_pass(_head(store), read_time), tid)
    assert _signature_count(store, tid) == 0


def test_record_reports_sign_failed_and_a_close_has_nothing_to_reuse(store):
    tid = _in_progress_ticket()
    sha = _head(store)
    read_time = current_material_fingerprint(tid)
    rebar.edit_ticket(tid, description="## Plan\nD2 edited during the run\n")

    outcome = transition_close.record_completion_verdict(_pass(sha, read_time), tid)

    assert outcome["signed"] is False
    assert outcome["cause"] == "sign_failed"
    assert "material changed" in outcome["error"]
    assert close_autoresume._reusable_attested_pass(tid, ref=sha, repo_root=str(store)) is None


def test_unchanged_material_signs_and_binds_the_read_time_material(store):
    tid = _in_progress_ticket()
    read_time = current_material_fingerprint(tid)

    record = transition_close.sign_completion_verdict(_pass(_head(store), read_time), tid)

    assert f"material: {read_time}" in record["manifest"]
    assert rebar.verify_signature(tid, kind="completion-verifier")["verdict"] == "certified"
