"""Runbook coverage for SSM-backed GitHub Actions secret mirrors."""

from __future__ import annotations

from pathlib import Path

_RUNBOOK = Path("infra/runbooks/ssm-secret-write-only.md")
_CROSS_REFERENCE_RUNBOOKS = (
    Path("infra/runbooks/mcp-client-pats.md"),
    Path("infra/runbooks/provision-restore.md"),
)


def test_secret_rotation_runbook_documents_actions_mirrors() -> None:
    text = _RUNBOOK.read_text(encoding="utf-8")

    assert "Sync mirrored GitHub Actions secrets" in text
    assert "2026-09-07" in text
    for secret, param in {
        "ANTHROPIC_API_KEY": "/rebar/prod/anthropic-api-key",
        "JIRA_API_TOKEN": "/rebar/prod/jira-api-token",
        "GERRIT_SSH_PRIVKEY": "/rebar/prod/ci-gerrit-ssh-key",
        "REBAR_BOT_SIGNING_KEY": "/rebar/prod/rebar-bot-signing-key",
        "OPENAI_API_KEY": "GitHub-only",
    }.items():
        assert secret in text
        assert param in text


def test_secret_rotation_runbook_has_copy_paste_sync_commands() -> None:
    text = _RUNBOOK.read_text(encoding="utf-8")

    for secret, param in {
        "ANTHROPIC_API_KEY": "/rebar/prod/anthropic-api-key",
        "JIRA_API_TOKEN": "/rebar/prod/jira-api-token",
        "GERRIT_SSH_PRIVKEY": "/rebar/prod/ci-gerrit-ssh-key",
        "REBAR_BOT_SIGNING_KEY": "/rebar/prod/rebar-bot-signing-key",
    }.items():
        assert param in text.split(f"gh secret set {secret} --repo navapbc/rebar")[0]
        assert f"gh secret set {secret} --repo navapbc/rebar" in text

    assert "printf '%s' \"$secret_value\"" in text

    assert "LastModifiedDate" in text
    assert "gh secret list --repo navapbc/rebar --json name,updatedAt" in text


def test_secret_rotation_checklist_is_cross_referenced() -> None:
    target = "ssm-secret-write-only.md#sync-mirrored-github-actions-secrets"
    for runbook in _CROSS_REFERENCE_RUNBOOKS:
        assert target in runbook.read_text(encoding="utf-8")
