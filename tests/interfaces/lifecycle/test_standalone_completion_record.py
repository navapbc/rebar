"""A completion verifier run made OUTSIDE a close records its result on the ticket, and a
later same-``--ref`` close REUSES that certified PASS instead of re-running the verifier.

This pins the recording half (``transition_close.record_completion_verdict``) that composes
with the already-tested close-time reuse (``close_autoresume._reusable_attested_pass``,
covered by ``test_completion_attestation_reuse``)."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import pytest

import rebar
import rebar.llm
from rebar._cli._llm_commands import _render_record_line, _verify_completion
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


def test_no_verified_sha_pass_is_not_signed(rebar_repo: Path) -> None:
    """An attested, certifiable PASS with no ``verified_at_sha`` to bind records only the
    sidecar — there is no sha for a later close to re-check, so it must not mint a cert."""
    ticket = _make_ticket(rebar_repo)
    result = _attested_pass("")  # attested + certifiable, but no sha
    result["verified_at_sha"] = ""

    outcome = record_completion_verdict(result, ticket, repo_root=str(rebar_repo))

    assert outcome["signed"] is False
    assert outcome["cause"] == "no_verified_sha"
    assert outcome["sidecar_written"] is True
    stored = rebar.verify_signature(ticket, kind="completion-verifier", repo_root=str(rebar_repo))
    assert stored["verdict"] != "certified"


def test_render_record_line_reports_signed_and_causes(capsys: pytest.CaptureFixture[str]) -> None:
    """The operator-facing ``recorded:`` line names the signed case and each not-signed cause."""
    _render_record_line({"signed": True})
    assert "signed a completion-verifier attestation" in capsys.readouterr().out

    _render_record_line({"signed": False, "cause": "sign_disabled", "sidecar_written": True})
    out = capsys.readouterr().out
    assert "not signed (--no-sign)" in out
    assert "sidecar recorded" in out

    _render_record_line(
        {"signed": False, "cause": "sign_failed", "error": "boom", "sidecar_written": False}
    )
    out = capsys.readouterr().out
    assert "signing FAILED: boom" in out
    assert "sidecar NOT recorded" in out


def _cli_provider(monkeypatch: pytest.MonkeyPatch, sha: str) -> None:
    def provider(ticket_id, *, graph=None, ref=None, source=None):
        return _attested_pass(sha)

    monkeypatch.setattr(rebar.llm, "verify_completion", provider)


def test_cli_no_sign_records_only_sidecar(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`rebar verify-completion <id> --no-sign` on an attested PASS records the sidecar but
    signs no attestation, so a later same-ref close still runs a fresh verification."""
    _enable_gate(rebar_repo)
    target = _git(rebar_repo, "rev-parse", "HEAD")
    ticket = _make_ticket(rebar_repo)
    _cli_provider(monkeypatch, target)

    rc = _verify_completion([ticket, "--no-sign"])

    assert rc == 0
    stored = rebar.verify_signature(ticket, kind="completion-verifier", repo_root=str(rebar_repo))
    assert stored["verdict"] != "certified"
    assert completion_sidecar.latest_pass_record(ticket, repo_root=str(rebar_repo)) is not None

    calls: list[str] = []
    _provider_spy(monkeypatch, calls)
    _close(rebar_repo, ticket, target)
    assert calls == [target]


def test_cli_default_signs_the_attestation(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`rebar verify-completion <id>` (no --no-sign) on an attested PASS signs the reusable
    attestation, and a later same-ref close reuses it (verifier provider never re-invoked)."""
    _enable_gate(rebar_repo)
    target = _git(rebar_repo, "rev-parse", "HEAD")
    ticket = _make_ticket(rebar_repo)
    _cli_provider(monkeypatch, target)

    rc = _verify_completion([ticket])

    assert rc == 0
    stored = rebar.verify_signature(ticket, kind="completion-verifier", repo_root=str(rebar_repo))
    assert stored["verdict"] == "certified"

    calls: list[str] = []
    _provider_spy(monkeypatch, calls)
    _close(rebar_repo, ticket, target)
    assert calls == []


def _cli_fail(sha: str, *, insufficient: bool) -> dict:
    """A standalone-verb FAIL verdict. ``insufficient`` marks an evidence-exhausted/truncated
    run (top-level ``evidence_sufficient`` False, mirroring what ``reconcile_verdict`` stamps);
    otherwise a positively-refuted criterion (no top-level marker)."""
    result = {
        "verdict": "FAIL",
        "findings": [
            {
                "criterion": "U0",
                "severity": "high",
                "dimension": "completion",
                "detail": (
                    "insufficient evidence (search exhausted)"
                    if insufficient
                    else "positively refuted"
                ),
            }
        ],
        "criteria": [{"criterion": "U0", "met": False}],
        "runner": "standalone-runner",
        "model": "standalone-model",
        "source": "attested",
        "verified_at_sha": sha,
        "signable": False,
        "certifiable": True,
        "target": {"kind": "ticket", "ticket_ids": ["t"]},
    }
    if insufficient:
        result["evidence_sufficient"] = False
    return result


def _cli_fail_provider(monkeypatch: pytest.MonkeyPatch, sha: str, *, insufficient: bool) -> None:
    def provider(ticket_id, *, graph=None, ref=None, source=None):
        return _cli_fail(sha, insufficient=insufficient)

    monkeypatch.setattr(rebar.llm, "verify_completion", provider)


def test_cli_insufficiency_only_fail_returns_retryable_exit_11(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bug 2dcb — end-to-end at the standalone ``verify-completion`` verb: an insufficiency-only
    FAIL (top-level ``evidence_sufficient`` False) returns the RETRYABLE exit 11 via the shared
    ``completion_fail_returncode`` helper, so a caller scripting the verb retries instead of
    treating an exhausted/truncated search as ``criteria unmet``."""
    _enable_gate(rebar_repo)
    target = _git(rebar_repo, "rev-parse", "HEAD")
    ticket = _make_ticket(rebar_repo)
    _cli_fail_provider(monkeypatch, target, insufficient=True)

    rc = _verify_completion([ticket, "--no-sign"])

    assert rc == 11


def test_cli_refutation_fail_returns_hard_block_exit_1(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control for bug 2dcb: a positively-refuted FAIL (no top-level insufficiency marker) stays
    the hard-block exit 1 — the retryable exit 11 must not swallow a genuine criteria failure."""
    _enable_gate(rebar_repo)
    target = _git(rebar_repo, "rev-parse", "HEAD")
    ticket = _make_ticket(rebar_repo)
    _cli_fail_provider(monkeypatch, target, insufficient=False)

    rc = _verify_completion([ticket, "--no-sign"])

    assert rc == 1


class _FakeMCP:
    """Collects decorated tool callables by name (mirrors the read/write registrar tests)."""

    def __init__(self) -> None:
        self.tools: dict = {}

    def tool(self, *_a, **_k):
        def _decorate(fn):
            self.tools[fn.__name__] = fn
            return fn

        return _decorate


class _FakeCtx:
    def __init__(self, *, readonly: bool) -> None:
        self._readonly = readonly
        self.logger = logging.getLogger("test")

    def allow_llm(self) -> bool:
        return True

    def readonly(self) -> bool:
        return self._readonly


def _mcp_verify_tool(readonly: bool):
    from rebar._mcp_llm import register_llm_tools

    mcp = _FakeMCP()
    register_llm_tools(mcp, _FakeCtx(readonly=readonly))
    return mcp.tools["verify_completion"]


def test_mcp_writable_server_signs(rebar_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A WRITABLE MCP server records: an attested PASS signs the reusable attestation and the
    result carries a ``record`` field describing it."""
    monkeypatch.setenv("REBAR_MCP_ALLOW_LLM", "1")
    target = _git(rebar_repo, "rev-parse", "HEAD")
    ticket = _make_ticket(rebar_repo)
    monkeypatch.setattr(rebar.llm, "verify_completion", lambda t, **k: _attested_pass(target))

    result = _mcp_verify_tool(readonly=False)(ticket)

    assert result["record"]["signed"] is True
    stored = rebar.verify_signature(ticket, kind="completion-verifier", repo_root=str(rebar_repo))
    assert stored["verdict"] == "certified"


def test_mcp_readonly_server_records_nothing(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A READ-ONLY MCP server must mutate NOTHING: no sidecar (an append_event) and no
    signature, even on an attested PASS. The ``record`` field reports ``read_only``."""
    monkeypatch.setenv("REBAR_MCP_ALLOW_LLM", "1")
    target = _git(rebar_repo, "rev-parse", "HEAD")
    ticket = _make_ticket(rebar_repo)
    monkeypatch.setattr(rebar.llm, "verify_completion", lambda t, **k: _attested_pass(target))

    result = _mcp_verify_tool(readonly=True)(ticket)

    assert result["record"] == {
        "signed": False,
        "cause": "read_only",
        "sidecar_written": False,
        "error": "",
    }
    stored = rebar.verify_signature(ticket, kind="completion-verifier", repo_root=str(rebar_repo))
    assert stored["verdict"] != "certified"
    assert completion_sidecar.latest_pass_record(ticket, repo_root=str(rebar_repo)) is None
