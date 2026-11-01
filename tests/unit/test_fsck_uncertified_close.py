"""fsck reports a completion-required close that carries no completion certificate.

Bug external-taxonomic-iguanodon: on the non-bundled close path the STATUS commit and the
completion signature are two writes, so a process killed between them leaves a ticket that
is closed, ``completion_expectation=required``, and uncertified. That must be detectable
rather than silently resembling a forced close (which records ``force_bypassed``).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
from rebar._commands import transition_close
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


def _close(store: Path, monkeypatch, title: str, expectation: str, *, sign: str) -> str:
    """Close one ticket through the real close path with a stubbed verifier.

    ``sign`` is ``"real"`` (the PASS is signed), ``"fail"`` (signing degrades to
    ``sign_failed`` after STATUS committed), or ``"none"`` (no verdict to sign).
    """
    tid = rebar.create_ticket("task", title, description="## Plan\nD1\n")
    rebar.claim(tid, assignee="t@e.com")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=store, check=True, capture_output=True, text=True
    ).stdout.strip()
    verdict = None
    if sign != "none":
        verdict = {
            "verdict": "PASS",
            "model": "m",
            "runner": "r",
            "source": "attested",
            "certifiable": True,
            "verified_at_sha": head,
            "material_fingerprint": current_material_fingerprint(tid),
        }
    with monkeypatch.context() as m:
        m.setattr(transition_close, "_completion_precheck", lambda *a, **k: (verdict, expectation))
        if sign == "fail":
            m.setattr(
                transition_close,
                "_sign_completion_and_report",
                lambda *a, **k: {"signed": False, "cause": "sign_failed", "error": "key gone"},
            )
        rebar.transition(tid, "in_progress", "closed")
    return tid


def _uncertified(payload: dict) -> list[dict]:
    return [i for i in payload["issues"] if i["kind"] == "uncertified_close"]


def test_fsck_flags_a_required_close_without_a_completion_certificate(store, monkeypatch):
    unsigned = _close(store, monkeypatch, "lost signature", "required", sign="fail")

    payload = rebar.fsck_report(report_only=True, repo_root=str(store))

    found = _uncertified(payload)
    assert [i["ticket_id"] for i in found] == [unsigned]
    assert found[0]["counted"] is False
    assert "verify-completion" in found[0]["detail"]
    assert payload["issue_count"] == 0, "historical findings must not fail every fsck run"


def test_fsck_does_not_flag_signed_forced_or_disposition_closes(store, monkeypatch):
    _close(store, monkeypatch, "signed close", "required", sign="real")
    _close(store, monkeypatch, "forced close", "force_bypassed", sign="none")
    _close(store, monkeypatch, "disposition close", "disposition", sign="none")

    payload = rebar.fsck_report(report_only=True, repo_root=str(store))

    assert _uncertified(payload) == []
