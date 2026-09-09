from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path

from _subprocess_env import subprocess_env

SCRIPT = Path(__file__).resolve().parents[2] / "infra" / "scripts" / "observability.sh"
_SHA = "a" * 40


def _stub(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body))
    path.chmod(0o755)


def test_reviewbot_webhook_auth_rejections_publish_from_health_payload(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    aws_log = tmp_path / "aws.log"
    health = json.dumps(
        {
            "status": "ok",
            "in_flight": 0,
            "queue_depth": 0,
            "gerrit_auth": "ok",
            "webhook_auth_rejections": 3,
            "webhook_auth_last_rejected_age_seconds": 12,
        }
    )
    _stub(
        bin_dir,
        "curl",
        f"""
        case "$*" in
          *169.254.169.254*/latest/api/token*) printf 'dummy-token'; exit 0 ;;
          *169.254.169.254*/placement/region*) printf 'us-east-1'; exit 0 ;;
          *169.254.169.254*/instance-id*) printf 'i-1234567890abcdef0'; exit 0 ;;
          *'/review/health'*|*'/review/health '*) printf '%s\\n200' '{health}'; exit 0 ;;
          *'/config/server/version'*) printf '200'; exit 0 ;;
          *projects/rebar/branches/main*)
            printf ")]}}'\\n"; printf '{{"revision": "{_SHA}"}}\\n'; exit 0 ;;
        esac
        printf '200'
        """,
    )
    _stub(bin_dir, "git", f'printf "{_SHA}\\trefs/heads/main\\n"; exit 0')
    _stub(bin_dir, "logger", "exit 0")
    _stub(bin_dir, "journalctl", "exit 0")
    _stub(bin_dir, "du", "printf '0\\t%s\\n' \"${1:-.}\"")
    _stub(bin_dir, "docker", "exit 0")
    _stub(bin_dir, "findmnt", "exit 1")
    _stub(bin_dir, "timeout", 'exec "$@"')
    _stub(bin_dir, "aws", 'printf \'%s\\n\' "$*" >> "$AWS_LOG"; exit 0')

    env = subprocess_env()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "AWS_LOG": str(aws_log),
            "REPL_LOG": str(tmp_path / "missing-replication.log"),
            "REGION_CACHE": str(tmp_path / "probe-region"),
            "REVIEWBOT_WEBHOOK_AUTH_OFFSET_FILE": str(tmp_path / "reviewbot-webhook-auth-offset"),
            "PROBE_DEADLINE_SEC": "20",
            "PROBE_TAIL_RESERVE_SEC": "1",
        }
    )

    result = subprocess.run(["bash", str(SCRIPT)], env=env, timeout=60, check=False)
    repeated = subprocess.run(["bash", str(SCRIPT)], env=env, timeout=60, check=False)

    assert (result.returncode, repeated.returncode) == (0, 0)
    published = [
        line
        for line in aws_log.read_text().splitlines()
        if "--metric-name reviewbot_webhook_auth_rejections" in line
    ]
    assert any(
        "--metric-name reviewbot_webhook_auth_rejections --unit Count --value 3" in line
        for line in published
    )
    assert any(
        "--metric-name reviewbot_webhook_auth_rejections --unit Count --value 0" in line
        for line in published
    )


def test_reviewbot_webhook_auth_rejections_survive_process_counter_reset(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    aws_log = tmp_path / "aws.log"
    health = json.dumps(
        {
            "status": "ok",
            "in_flight": 0,
            "queue_depth": 0,
            "gerrit_auth": "ok",
            "webhook_auth_rejections": 2,
            "webhook_auth_last_rejected_age_seconds": 12,
        }
    )
    _stub(
        bin_dir,
        "curl",
        f"""
        case "$*" in
          *169.254.169.254*/latest/api/token*) printf 'dummy-token'; exit 0 ;;
          *169.254.169.254*/placement/region*) printf 'us-east-1'; exit 0 ;;
          *169.254.169.254*/instance-id*) printf 'i-1234567890abcdef0'; exit 0 ;;
          *'/review/health'*|*'/review/health '*) printf '%s\\n200' '{health}'; exit 0 ;;
          *'/config/server/version'*) printf '200'; exit 0 ;;
          *projects/rebar/branches/main*)
            printf ")]}}'\\n"; printf '{{"revision": "{_SHA}"}}\\n'; exit 0 ;;
        esac
        printf '200'
        """,
    )
    _stub(bin_dir, "git", f'printf "{_SHA}\\trefs/heads/main\\n"; exit 0')
    _stub(bin_dir, "logger", "exit 0")
    _stub(bin_dir, "journalctl", "exit 0")
    _stub(bin_dir, "du", "printf '0\\t%s\\n' \"${1:-.}\"")
    _stub(bin_dir, "docker", "exit 0")
    _stub(bin_dir, "findmnt", "exit 1")
    _stub(bin_dir, "timeout", 'exec "$@"')
    _stub(bin_dir, "aws", 'printf \'%s\\n\' "$*" >> "$AWS_LOG"; exit 0')

    offset = tmp_path / "reviewbot-webhook-auth-offset"
    offset.write_text("17\n")
    env = subprocess_env()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "AWS_LOG": str(aws_log),
            "REPL_LOG": str(tmp_path / "missing-replication.log"),
            "REGION_CACHE": str(tmp_path / "probe-region"),
            "REVIEWBOT_WEBHOOK_AUTH_OFFSET_FILE": str(offset),
            "PROBE_DEADLINE_SEC": "20",
            "PROBE_TAIL_RESERVE_SEC": "1",
        }
    )

    result = subprocess.run(["bash", str(SCRIPT)], env=env, timeout=60, check=False)

    assert result.returncode == 0
    published = [
        line
        for line in aws_log.read_text().splitlines()
        if "--metric-name reviewbot_webhook_auth_rejections" in line
    ]
    assert any(
        "--metric-name reviewbot_webhook_auth_rejections --unit Count --value 2" in line
        for line in published
    )
