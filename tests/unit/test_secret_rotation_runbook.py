from pathlib import Path


def test_rotation_runbook_maps_actions_secret_mirrors_to_ssm_sources():
    text = Path("infra/runbooks/ssm-secret-write-only.md").read_text()

    expected_rows = {
        "| `ANTHROPIC_API_KEY` | GitHub Actions secret | `/rebar/prod/anthropic-api-key` |",
        "| `JIRA_API_TOKEN` | GitHub Actions secret | `/rebar/prod/jira-api-token` |",
        "| `GERRIT_SSH_PRIVKEY` | GitHub Actions secret | `/rebar/prod/ci-gerrit-ssh-key` |",
        "| `OPENAI_API_KEY` | GitHub Actions secret | GitHub-only |",
        "| `REBAR_BOT_SIGNING_KEY` | GitHub Actions secret | `/rebar/prod/rebar-bot-signing-key` |",
    }

    for row in expected_rows:
        assert row in text


def test_rotation_runbook_syncs_actions_secret_mirrors_after_ssm_rotation():
    text = Path("infra/runbooks/ssm-secret-write-only.md").read_text()

    expected_commands = {
        "aws ssm get-parameter --with-decryption --name /rebar/prod/anthropic-api-key",
        "gh secret set ANTHROPIC_API_KEY --repo navapbc/rebar",
        "aws ssm get-parameter --with-decryption --name /rebar/prod/jira-api-token",
        "gh secret set JIRA_API_TOKEN --repo navapbc/rebar",
        "aws ssm get-parameter --with-decryption --name /rebar/prod/ci-gerrit-ssh-key",
        "gh secret set GERRIT_SSH_PRIVKEY --repo navapbc/rebar",
        "aws ssm get-parameter --with-decryption --name /rebar/prod/rebar-bot-signing-key",
        "gh secret set REBAR_BOT_SIGNING_KEY --repo navapbc/rebar",
    }

    for command in expected_commands:
        assert command in text
