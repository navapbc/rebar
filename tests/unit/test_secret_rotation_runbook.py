"""Runbook coverage for SSM-backed GitHub Actions secret mirrors."""

from __future__ import annotations

from pathlib import Path

_RUNBOOK = Path("infra/runbooks/ssm-secret-write-only.md")
_CROSS_REFERENCE_RUNBOOKS = (
    Path("infra/runbooks/mcp-client-pats.md"),
    Path("infra/runbooks/provision-restore.md"),
)
_MIRRORS = {
    "ANTHROPIC_API_KEY": "/rebar/prod/anthropic-api-key",
    "JIRA_API_TOKEN": "/rebar/prod/jira-api-token",
    "GERRIT_SSH_PRIVKEY": "/rebar/prod/ci-gerrit-ssh-key",
    "REBAR_BOT_SIGNING_KEY": "/rebar/prod/rebar-bot-signing-key",
}


def test_rotation_runbook_maps_actions_secret_mirrors_to_ssm_sources() -> None:
    text = _RUNBOOK.read_text(encoding="utf-8")

    expected_rows = {
        "| `ANTHROPIC_API_KEY` | GitHub Actions secret | `/rebar/prod/anthropic-api-key` |",
        "| `JIRA_API_TOKEN` | GitHub Actions secret | `/rebar/prod/jira-api-token` |",
        "| `GERRIT_SSH_PRIVKEY` | GitHub Actions secret | `/rebar/prod/ci-gerrit-ssh-key` |",
        "| `OPENAI_API_KEY` | GitHub Actions secret | GitHub-only |",
        "| `REBAR_BOT_SIGNING_KEY` | GitHub Actions secret | `/rebar/prod/rebar-bot-signing-key` |",
    }

    for row in expected_rows:
        assert row in text
    assert "2026-09-07" in text


def test_rotation_runbook_syncs_actions_secret_mirrors_after_ssm_rotation() -> None:
    text = _RUNBOOK.read_text(encoding="utf-8")

    for secret, param in _MIRRORS.items():
        command = f"aws ssm get-parameter --with-decryption --name {param}"
        assert command in text
        assert f"gh secret set {secret} --repo navapbc/rebar" in text
        assert param in text.split(f"gh secret set {secret} --repo navapbc/rebar")[0]

    assert "printf '%s' \"$secret_value\"" in text
    assert "LastModifiedDate" in text
    assert "gh secret list --repo navapbc/rebar --json name,updatedAt" in text


def test_secret_rotation_checklist_is_cross_referenced() -> None:
    target = "ssm-secret-write-only.md#github-actions-mirror-sync"
    for runbook in _CROSS_REFERENCE_RUNBOOKS:
        assert target in runbook.read_text(encoding="utf-8")
