"""Host logs must survive the host [rebar:9018-3385-ccfa-4568].

The 2026-09-04 outage left no evidence because the Gerrit host had no off-box log
destination, every health signal but the EC2 status checks came from the host itself, and
restarting it destroyed the only journal. ``infra/terraform/host_logs.tf`` closes that with
AWS-managed parts only: a log group with explicit retention, a CloudWatch agent installed and
configured by State Manager associations from an SSM parameter, an alarm that pages when the
log group stops receiving events, and a Route 53 health check that observes Gerrit from
outside the host.

These are offline text contracts over committed Terraform, the agent config and the runbook.
They do not contact AWS. Each guard over the agent config is seeded with the defect it rejects
so a checker that stopped matching fails instead of passing vacuously.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_TF_DIR = _ROOT / "infra" / "terraform"
_HOST_LOGS_TF = _TF_DIR / "host_logs.tf"
_AGENT_CONFIG = _TF_DIR / "cwagent" / "rebar-host.json"
_RUNBOOK = _ROOT / "infra" / "runbooks" / "gerrit-host-wedged-ssm-lost.md"

_LOG_GROUP = "/rebar/gerrit-host"


def _tf() -> str:
    return _HOST_LOGS_TF.read_text(encoding="utf-8")


def _block(source: str, rtype: str, label: str) -> str:
    """Body of one ``resource "<rtype>" "<label>"`` block, brace-matched from its header."""
    header = re.search(rf'resource\s+"{re.escape(rtype)}"\s+"{re.escape(label)}"\s*\{{', source)
    assert header, f'resource "{rtype}" "{label}" not found in {_HOST_LOGS_TF.name}'
    depth, index = 1, header.end()
    while depth and index < len(source):
        depth += {"{": 1, "}": -1}.get(source[index], 0)
        index += 1
    return source[header.end() : index - 1]


def _attr(body: str, name: str) -> str | None:
    """Raw right-hand side of a top-level ``name = value`` line, or ``None``."""
    match = re.search(rf"^[ \t]*{re.escape(name)}[ \t]*=[ \t]*(.+?)[ \t]*$", body, re.MULTILINE)
    return match.group(1) if match else None


def _agent_config() -> dict:
    return json.loads(_AGENT_CONFIG.read_text(encoding="utf-8"))


def _config_problems(config: dict) -> list[str]:
    """Every way an agent config fails the off-box shipping contract. Empty means it passes."""
    problems: list[str] = []
    collected = config.get("logs", {}).get("logs_collected", {})
    journald = collected.get("journald", {}).get("collect_list", [])
    files = collected.get("files", {}).get("collect_list", [])
    entries = [*journald, *files]
    if not entries:
        return ["no log sources configured"]
    units = {unit for entry in journald for unit in entry.get("units", [])}
    matches = [m for entry in journald for m in entry.get("matches", [])]
    if {"_TRANSPORT": "kernel"} not in matches:
        problems.append("kernel messages are not shipped (_TRANSPORT=kernel match missing)")
    for unit in ("docker.service", "nginx.service", "rebar-*"):
        if unit not in units:
            problems.append(f"journald unit {unit} is not shipped")
    paths = [entry.get("file_path", "") for entry in files]
    if "/var/log/nginx/error.log" not in paths:
        problems.append("nginx error.log is not shipped")
    if any("access" in path for path in paths):
        problems.append("nginx access.log must not be shipped")
    for entry in entries:
        if entry.get("log_group_name") != _LOG_GROUP:
            problems.append(f"entry ships to {entry.get('log_group_name')!r}, not {_LOG_GROUP}")
        if "retention_in_days" in entry:
            problems.append("retention is owned by Terraform; the agent must not set it")
    return problems


# ──────────────────────────────── agent config ────────────────────────────────


def test_agent_config_ships_every_required_source_to_the_terraform_group() -> None:
    assert _config_problems(_agent_config()) == []


def test_agent_config_runs_as_root_so_it_can_read_the_journal_and_nginx_logs() -> None:
    assert _agent_config().get("agent", {}).get("run_as_user") == "root"


def test_seeded_access_log_is_rejected() -> None:
    config = copy.deepcopy(_agent_config())
    config["logs"]["logs_collected"]["files"]["collect_list"].append(
        {"file_path": "/var/log/nginx/access.log", "log_group_name": _LOG_GROUP}
    )
    assert "nginx access.log must not be shipped" in _config_problems(config)


def test_seeded_missing_kernel_match_is_rejected() -> None:
    config = copy.deepcopy(_agent_config())
    for entry in config["logs"]["logs_collected"]["journald"]["collect_list"]:
        entry.pop("matches", None)
    assert any("kernel" in p for p in _config_problems(config))


def test_seeded_agent_side_retention_is_rejected() -> None:
    config = copy.deepcopy(_agent_config())
    config["logs"]["logs_collected"]["journald"]["collect_list"][0]["retention_in_days"] = 7
    assert any("retention" in p for p in _config_problems(config))


def test_seeded_empty_config_is_rejected() -> None:
    assert _config_problems({}) == ["no log sources configured"]


# ────────────────────────────────── terraform ─────────────────────────────────


def test_log_group_has_explicit_ninety_day_retention() -> None:
    body = _block(_tf(), "aws_cloudwatch_log_group", "gerrit_host")
    assert _attr(body, "retention_in_days") == "90"
    assert f'host_log_group_name = "{_LOG_GROUP}"' in " ".join(_tf().split())


def test_agent_config_parameter_is_readable_under_the_agent_prefix_and_loads_the_file() -> None:
    body = _block(_tf(), "aws_ssm_parameter", "cwagent_config")
    assert '"AmazonCloudWatch-' in _tf(), "the parameter name must start with AmazonCloudWatch-"
    assert _attr(body, "type") == '"String"'
    assert "cwagent/rebar-host.json" in (_attr(body, "value") or "")


def test_install_association_is_aws_managed_unpinned_and_scheduled() -> None:
    body = _block(_tf(), "aws_ssm_association", "cwagent_install")
    assert _attr(body, "name") == '"AWS-ConfigureAWSPackage"'
    assert re.search(r'name\s*=\s*"AmazonCloudWatchAgent"', body)
    assert re.search(r'action\s*=\s*"Install"', body)
    assert not re.search(r"^\s*version\s*=", body, re.MULTILINE), "the agent version is unpinned"
    assert _attr(body, "schedule_expression"), "a rebuilt host needs a scheduled install"


def test_configure_association_applies_the_parameter_on_a_schedule() -> None:
    body = _block(_tf(), "aws_ssm_association", "cwagent_configure")
    assert _attr(body, "name") == '"AmazonCloudWatch-ManageAgent"'
    assert re.search(r'action\s*=\s*"configure"', body)
    assert re.search(r'optionalConfigurationSource\s*=\s*"ssm"', body)
    assert re.search(r"optionalConfigurationLocation\s*=\s*aws_ssm_parameter\.cwagent_config", body)
    assert re.search(r'optionalRestart\s*=\s*"yes"', body)
    assert _attr(body, "schedule_expression")


@pytest.mark.parametrize("label", ["cwagent_install", "cwagent_configure"])
def test_associations_target_the_host_by_name_tag(label: str) -> None:
    body = _block(_tf(), "aws_ssm_association", label)
    assert re.search(r'key\s*=\s*"tag:Name"', body)
    assert re.search(r"values\s*=\s*\[local\.host_name_tag\]", body)
    # The same tag main.tf puts on the instance and monitoring.tf's data source filters on.
    assert re.search(r'host_name_tag\s*=\s*"rebar-gerrit"', _tf())


def test_instance_role_grant_is_scoped_to_the_group_and_the_parameter() -> None:
    body = _block(_tf(), "aws_iam_role_policy", "host_logs_ship")
    assert _attr(body, "role") == "aws_iam_role.gerrit_instance.id"
    doc = _tf()
    for action in ("logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"):
        assert f'"{action}"' in doc
    assert '"ssm:GetParameter"' in doc
    assert "aws_cloudwatch_log_group.gerrit_host.arn" in doc
    assert "aws_ssm_parameter.cwagent_config.arn" in doc
    assert not re.search(r'resources\s*=\s*\[\s*"\*"\s*\]', doc), "no wildcard resource"


def _alarm(label: str) -> str:
    return _block(_tf(), "aws_cloudwatch_metric_alarm", label)


def test_shipping_stopped_alarm_pages_on_silence() -> None:
    body = _alarm("host_logs_not_arriving")
    assert _attr(body, "namespace") == '"AWS/Logs"'
    assert _attr(body, "metric_name") == '"IncomingLogEvents"'
    assert "LogGroupName = aws_cloudwatch_log_group.gerrit_host.name" in " ".join(body.split())
    assert _attr(body, "treat_missing_data") == '"breaching"'
    assert _attr(body, "datapoints_to_alarm") == _attr(body, "evaluation_periods")
    assert _attr(body, "alarm_actions") == "[aws_sns_topic.alerts.arn]"


def test_external_health_check_probes_gerrit_from_outside_the_host() -> None:
    check = _block(_tf(), "aws_route53_health_check", "gerrit_https")
    assert _attr(check, "type") == '"HTTPS"'
    assert _attr(check, "fqdn") == "var.dns_name"
    assert _attr(check, "resource_path") == '"/config/server/version"'
    assert _attr(check, "port") == "443"

    body = _alarm("gerrit_external_probe_down")
    assert _attr(body, "namespace") == '"AWS/Route53"'
    assert _attr(body, "metric_name") == '"HealthCheckStatus"'
    assert "HealthCheckId = aws_route53_health_check.gerrit_https.id" in " ".join(body.split())
    assert _attr(body, "treat_missing_data") == '"breaching"'
    assert _attr(body, "datapoints_to_alarm") == _attr(body, "evaluation_periods")
    assert _attr(body, "alarm_actions") == "[aws_sns_topic.alerts.arn]"


# ─────────────────────────────────── runbook ──────────────────────────────────


def test_runbook_captures_console_before_any_restart_and_names_the_log_group() -> None:
    text = _RUNBOOK.read_text(encoding="utf-8")
    capture = re.search(r"get-console-output[^\n]*--latest[^\n]*>", text)
    assert capture, "the runbook must SAVE `get-console-output --latest` to a file"
    first_restart = min(
        text.find(cmd) for cmd in ("reboot-instances", "stop-instances") if cmd in text
    )
    assert capture.start() < first_restart, "the capture must precede every restart command"
    assert _LOG_GROUP in text
    assert "aws logs tail" in text
