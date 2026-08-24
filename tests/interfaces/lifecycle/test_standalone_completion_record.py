"""A completion verifier run made OUTSIDE a close records its result on the ticket, and a
later same-``--ref`` close REUSES that certified PASS instead of re-running the verifier.

This pins the recording half (``transition_close.record_completion_verdict``) that composes
with the already-tested close-time reuse (``close_autoresume._reusable_attested_pass``,
covered by ``test_completion_attestation_reuse``)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
import rebar.llm
from rebar._commands.transition_close import record_completion_verdict
from rebar.llm import completion_sidecar

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
        "record a standalone completion verdict",
        description=_DESCRIPTION,
        repo_root=str(repo),
    )
    rebar.claim(ticket, assignee="test", repo_root=str(repo))
    return ticket


def _attested_pass(sha: str, *, source: str = "attested", certifiable: bool = True) -> dict:
    return {
        "verdict": "PASS",
        "findings": [],
        "criteria": [],
        "runner": "standalone-runner",
        "model": "standalone-model",
        "source": source,
        "verified_at_sha": sha,
        "signable": source == "attested",
        "certifiable": certifiable,
        "target": {"kind": "ticket", "ticket_ids": ["t"]},
    }


def _provider_spy(monkeypatch: pytest.MonkeyPatch, calls: list[str]) -> None:
    from rebar._snapshot.repo_snapshot import resolve_ref

    def provider(ticket_id, *, ref=None, repo_root=None, **kwargs):
        target = resolve_ref(ref or "HEAD", repo_root, fetch=False)
        calls.append(target)
        return _attested_pass(target)

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


def test_attested_pass_is_signed_and_close_reuses_it(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A standalone attested PASS signs a completion-verifier op-cert + emits the PASS sidecar,
    and a later close at the SAME ref reuses it — the verifier provider is never invoked."""
    _enable_gate(rebar_repo)
    target = _git(rebar_repo, "rev-parse", "HEAD")
    ticket = _make_ticket(rebar_repo)

    outcome = record_completion_verdict(_attested_pass(target), ticket, repo_root=str(rebar_repo))

    assert outcome["signed"] is True
    assert outcome["cause"] == "signed"
    assert outcome["sidecar_written"] is True
    stored = rebar.verify_signature(ticket, kind="completion-verifier", repo_root=str(rebar_repo))
    assert stored["verdict"] == "certified"
    assert stored["verified_at_sha"] == target
    assert completion_sidecar.latest_pass_record(ticket, repo_root=str(rebar_repo)) is not None

    calls: list[str] = []
    _provider_spy(monkeypatch, calls)
    _close(rebar_repo, ticket, target)

    assert calls == []
    assert rebar.show_ticket(ticket, repo_root=str(rebar_repo))["status"] == "closed"


def test_no_sign_records_only_the_sidecar(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``sign=False`` (the CLI ``--no-sign`` / MCP read-only opt-out) emits the sidecar but
    signs no attestation, so a later close runs a fresh verification."""
    _enable_gate(rebar_repo)
    target = _git(rebar_repo, "rev-parse", "HEAD")
    ticket = _make_ticket(rebar_repo)

    outcome = record_completion_verdict(
        _attested_pass(target), ticket, repo_root=str(rebar_repo), sign=False
    )

    assert outcome["signed"] is False
    assert outcome["cause"] == "sign_disabled"
    assert outcome["sidecar_written"] is True
    stored = rebar.verify_signature(ticket, kind="completion-verifier", repo_root=str(rebar_repo))
    assert stored["verdict"] != "certified"

    calls: list[str] = []
    _provider_spy(monkeypatch, calls)
    _close(rebar_repo, ticket, target)

    assert calls == [target]


def test_local_source_is_never_signed(rebar_repo: Path) -> None:
    """A ``--source local`` verdict is unattested and must never mint a reusable certification."""
    target = _git(rebar_repo, "rev-parse", "HEAD")
    ticket = _make_ticket(rebar_repo)

    outcome = record_completion_verdict(
        _attested_pass(target, source="local"), ticket, repo_root=str(rebar_repo)
    )

    assert outcome["signed"] is False
    assert outcome["cause"] == "local_source"
    stored = rebar.verify_signature(ticket, kind="completion-verifier", repo_root=str(rebar_repo))
    assert stored["verdict"] != "certified"


def test_uncertifiable_pass_is_not_signed(rebar_repo: Path) -> None:
    """A ``certifiable=False`` PASS (an uncertified descendant withheld it) records only the
    sidecar, matching the close gate's own suppression."""
    target = _git(rebar_repo, "rev-parse", "HEAD")
    ticket = _make_ticket(rebar_repo)

    outcome = record_completion_verdict(
        _attested_pass(target, certifiable=False), ticket, repo_root=str(rebar_repo)
    )

    assert outcome["signed"] is False
    assert outcome["cause"] == "not_certifiable"


def test_fail_emits_sidecar_without_signing(rebar_repo: Path) -> None:
    """A FAIL leaves the durable COMPLETION_VERDICT sidecar but signs no attestation."""
    ticket = _make_ticket(rebar_repo)
    fail = {
        "verdict": "FAIL",
        "ticket_id": ticket,
        "findings": [{"criterion": "resolved", "detail": "not fixed", "severity": "high"}],
        "runner": "standalone-runner",
        "model": "standalone-model",
        "source": "attested",
    }

    outcome = record_completion_verdict(fail, ticket, repo_root=str(rebar_repo))

    assert outcome["signed"] is False
    assert outcome["cause"] == "not_pass"
    assert outcome["sidecar_written"] is True
    assert completion_sidecar.latest_fail_verdict(ticket, repo_root=str(rebar_repo)) is not None
    stored = rebar.verify_signature(ticket, kind="completion-verifier", repo_root=str(rebar_repo))
    assert stored["verdict"] != "certified"
