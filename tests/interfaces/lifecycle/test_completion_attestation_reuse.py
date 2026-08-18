"""Close-time reuse of a stored completion attestation (ticket e671-56bf-4022-46b5)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
import rebar.llm
from rebar import signing
from rebar._commands import transition as transition_command
from rebar._commands.transition_close import sign_completion_verdict
from rebar._snapshot.repo_snapshot import resolve_ref
from rebar.llm import completion_sidecar
from rebar.llm.plan_review import attest

_DESCRIPTION = "## Acceptance Criteria\n- [x] the reported defect is resolved\n"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _enable_gate(repo: Path, *, enabled: bool = True) -> None:
    value = "true" if enabled else "false"
    (repo / "rebar.toml").write_text(
        f"[verify]\nrequire_completion_verification_for_close = {value}\n"
    )


def _make_ticket(repo: Path) -> str:
    ticket = rebar.create_ticket(
        "bug",
        "reuse a stored completion attestation",
        description=_DESCRIPTION,
        repo_root=str(repo),
    )
    rebar.claim(ticket, assignee="test", repo_root=str(repo))
    return ticket


def _stored_pass(repo: Path, ticket: str, sha: str) -> dict:
    sign_completion_verdict(
        {
            "verdict": "PASS",
            "runner": "stored-runner",
            "model": "stored-model",
            "source": "attested",
            "verified_at_sha": sha,
            "certifiable": True,
        },
        ticket,
        repo_root=str(repo),
    )
    stored = rebar.verify_signature(ticket, kind="completion-verifier", repo_root=str(repo))
    assert stored["verdict"] == "certified"
    assert stored["verified_at_sha"] == sha
    assert attest.manifest_material(stored["manifest"])
    return stored


def _custom_completion_record(
    repo: Path,
    ticket: str,
    sha: str,
    *,
    verdict: str = "PASS",
    include_sha: bool = True,
    include_material: bool = True,
) -> None:
    manifest = [f"completion-verifier: {verdict}", f"ticket: {ticket}"]
    if include_material:
        material = attest.current_material_fingerprint(ticket, repo_root=str(repo))
        assert material
        manifest.append(f"material: {material}")
    if include_sha:
        manifest.append(signing.verified_at_sha_step(sha))
    signing.sign_manifest(
        ticket,
        manifest,
        kind="completion-verifier",
        repo_root=str(repo),
    )


def _provider_spy(monkeypatch: pytest.MonkeyPatch, calls: list[str]) -> None:
    def provider(ticket_id, *, ref=None, repo_root=None, **kwargs):
        target = resolve_ref(ref or "HEAD", repo_root, fetch=False)
        calls.append(target)
        return {
            "verdict": "PASS",
            "findings": [],
            "criteria": [],
            "runner": "fresh-runner",
            "model": "fresh-model",
            "source": "attested",
            "verified_at_sha": target,
            "certifiable": True,
        }

    monkeypatch.setattr(rebar.llm, "verify_completion", provider)


def _close(repo: Path, ticket: str, ref: str) -> None:
    rebar.transition(
        ticket,
        "in_progress",
        "closed",
        close_class="preexisting",
        ref=ref,
        repo_root=str(repo),
    )


def test_cli_close_reuses_valid_same_ref_completion_attestation(
    rebar_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The real CLI close path skips the provider and retains signed durable evidence."""
    _enable_gate(rebar_repo)
    target = _git(rebar_repo, "rev-parse", "HEAD")
    ticket = _make_ticket(rebar_repo)
    _stored_pass(rebar_repo, ticket, target)
    calls: list[str] = []
    _provider_spy(monkeypatch, calls)

    rc = transition_command.transition_cli(
        [
            ticket,
            "in_progress",
            "closed",
            "--class=preexisting",
            f"--ref={target}",
            "--output=json",
        ],
        repo_root=str(rebar_repo),
    )

    assert rc == 0, capsys.readouterr().err
    assert calls == []
    assert rebar.show_ticket(ticket, repo_root=str(rebar_repo))["status"] == "closed"
    closed_signature = rebar.verify_signature(
        ticket, kind="completion-verifier", repo_root=str(rebar_repo)
    )
    assert closed_signature["verdict"] == "certified"
    assert closed_signature["verified_at_sha"] == target
    assert completion_sidecar.latest_pass_record(ticket, repo_root=str(rebar_repo)) is not None


def test_changed_ref_runs_fresh_verification(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_gate(rebar_repo)
    old_sha = _git(rebar_repo, "rev-parse", "HEAD")
    ticket = _make_ticket(rebar_repo)
    _stored_pass(rebar_repo, ticket, old_sha)
    (rebar_repo / "later.txt").write_text("later\n")
    _git(rebar_repo, "add", "later.txt")
    _git(
        rebar_repo,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit",
        "-q",
        "-m",
        "later",
    )
    new_sha = _git(rebar_repo, "rev-parse", "HEAD")
    calls: list[str] = []
    _provider_spy(monkeypatch, calls)

    _close(rebar_repo, ticket, new_sha)

    assert calls == [new_sha]


@pytest.mark.parametrize(
    ("verdict", "include_sha", "include_material"),
    [
        ("FAIL", True, True),
        ("PASS", False, True),
        ("PASS", True, False),
    ],
)
def test_ineligible_completion_record_runs_fresh_verification(
    rebar_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    verdict: str,
    include_sha: bool,
    include_material: bool,
) -> None:
    _enable_gate(rebar_repo)
    target = _git(rebar_repo, "rev-parse", "HEAD")
    ticket = _make_ticket(rebar_repo)
    _custom_completion_record(
        rebar_repo,
        ticket,
        target,
        verdict=verdict,
        include_sha=include_sha,
        include_material=include_material,
    )
    calls: list[str] = []
    _provider_spy(monkeypatch, calls)

    _close(rebar_repo, ticket, target)

    assert calls == [target]


def test_wrong_kind_record_runs_fresh_verification(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_gate(rebar_repo)
    target = _git(rebar_repo, "rev-parse", "HEAD")
    ticket = _make_ticket(rebar_repo)
    signing.sign_manifest(
        ticket,
        ["plan-review: PASS", f"ticket: {ticket}"],
        kind="plan-review",
        repo_root=str(rebar_repo),
    )
    calls: list[str] = []
    _provider_spy(monkeypatch, calls)

    _close(rebar_repo, ticket, target)

    assert calls == [target]


def test_material_drift_runs_fresh_verification(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_gate(rebar_repo)
    target = _git(rebar_repo, "rev-parse", "HEAD")
    ticket = _make_ticket(rebar_repo)
    _stored_pass(rebar_repo, ticket, target)
    rebar.edit_ticket(
        ticket,
        description=_DESCRIPTION + "\nmaterial changed\n",
        repo_root=str(rebar_repo),
    )
    calls: list[str] = []
    _provider_spy(monkeypatch, calls)

    _close(rebar_repo, ticket, target)

    assert calls == [target]


def test_pre_reopen_attestation_runs_fresh_verification(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_gate(rebar_repo)
    target = _git(rebar_repo, "rev-parse", "HEAD")
    ticket = _make_ticket(rebar_repo)
    _stored_pass(rebar_repo, ticket, target)
    _enable_gate(rebar_repo, enabled=False)
    _close(rebar_repo, ticket, target)
    rebar.reopen(ticket, repo_root=str(rebar_repo))
    rebar.claim(ticket, assignee="test", repo_root=str(rebar_repo))
    _enable_gate(rebar_repo)
    calls: list[str] = []
    _provider_spy(monkeypatch, calls)

    _close(rebar_repo, ticket, target)

    assert calls == [target]


def test_unverifiable_completion_record_runs_fresh_verification(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_gate(rebar_repo)
    target = _git(rebar_repo, "rev-parse", "HEAD")
    ticket = _make_ticket(rebar_repo)
    _stored_pass(rebar_repo, ticket, target)
    real_verify = signing.verify_signature

    def mismatch(ticket_id, *, kind=None, repo_root=None):
        if kind == "completion-verifier":
            return {
                "ticket_id": ticket_id,
                "kind": kind,
                "verdict": "mismatch",
                "verified": False,
                "reason": "defect-seeded invalid signature",
                "manifest": [],
            }
        return real_verify(ticket_id, kind=kind, repo_root=repo_root)

    monkeypatch.setattr(signing, "verify_signature", mismatch)
    calls: list[str] = []
    _provider_spy(monkeypatch, calls)

    _close(rebar_repo, ticket, target)

    assert calls == [target]
