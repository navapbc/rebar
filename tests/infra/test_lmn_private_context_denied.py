from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TERRAFORM = ROOT / "infra" / "terraform" / "monitoring_lmn_private_context.tf"
HANDLER = ROOT / "infra" / "terraform" / "lmn-private-context-denied" / "handler.py"


class FakeCloudWatch:
    def __init__(self) -> None:
        self.metric_data: list[dict] = []

    def put_metric_data(self, **kwargs) -> None:
        self.metric_data.append(kwargs)


class FakeS3:
    def __init__(self, body: str) -> None:
        self.body = body

    def get_object(self, Bucket: str, Key: str) -> dict:
        return {"Body": types.SimpleNamespace(read=lambda: self.body.encode())}


class FakeAWS:
    def __init__(self, access_log: str) -> None:
        self.s3 = FakeS3(access_log)
        self.cloudwatch = FakeCloudWatch()

    def client(self, service: str, **kwargs):
        if service == "s3":
            return self.s3
        if service == "cloudwatch":
            return self.cloudwatch
        raise AssertionError(service)


def load_handler(monkeypatch: pytest.MonkeyPatch, access_log: str):
    fake = FakeAWS(access_log)
    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(client=fake.client))
    spec = importlib.util.spec_from_file_location(
        f"lmn_private_context_denied_{id(fake)}",
        HANDLER,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module, fake


def s3_record(requester: str, status: int = 403, error_code: str = "AccessDenied") -> str:
    return (
        "owner lmn-staging-private-context-896586841071 [18/Sep/2026:02:57:30 +0000] "
        f"192.0.2.10 {requester} REQID REST.GET.OBJECT policy/colorado.json "
        '"GET /policy/colorado.json HTTP/1.1" '
        f'{status} {error_code} 123 456 7 8 "-" "aws-sdk-js/3.895.0" - HOSTID SigV4 '
        "TLS_AES_128_GCM_SHA256 AuthHeader "
        "lmn-staging-private-context-896586841071.s3.amazonaws.com "
        "TLSv1.3 - -"
    )


def test_terraform_replaces_raw_s3_4xx_alarm_with_filtered_metric() -> None:
    text = TERRAFORM.read_text()

    assert 'lmn_private_context_alarm_name  = "lmn-staging-private-context-denied"' in text
    assert "alarm_name        = local.lmn_private_context_alarm_name" in text
    assert 'lmn_private_context_metric_ns   = "lmn/staging"' in text
    assert 'lmn_private_context_metric_name = "private_context_unexpected_denials"' in text
    assert "namespace   = local.lmn_private_context_metric_ns" in text
    assert "metric_name = local.lmn_private_context_metric_name" in text
    assert "threshold           = 5" in text
    assert "period              = 300" in text
    assert 'comparison_operator = "GreaterThanThreshold"' in text

    alarm_block = text.split('resource "aws_cloudwatch_metric_alarm"', 1)[1]
    assert '"AWS/S3"' not in alarm_block
    assert '"4xxErrors"' not in alarm_block
    assert "all-private-context-requests" not in alarm_block

    assert "lmn-staging-private-context-logs-896586841071" in text
    assert "access/" in text


def test_handler_ignores_expected_audit_denials_and_counts_unexpected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audit = (
        "arn:aws:sts::896586841071:assumed-role/lmn-staging-api/"
        "lmn-staging-private-audit-52d0cc282c73448b-api"
    )
    joe = "arn:aws:iam::896586841071:user/joe_frontier"
    unexpected = "arn:aws:sts::896586841071:assumed-role/lmn-staging-api/ordinary-session"
    access_log = "\n".join(
        [s3_record(audit) for _ in range(45)]
        + [s3_record(joe)]
        + [s3_record(unexpected) for _ in range(6)]
        + [
            s3_record(unexpected, status=404, error_code="NoSuchKey"),
            s3_record(unexpected, status=200, error_code="-"),
        ]
    )
    module, fake = load_handler(monkeypatch, access_log)

    result = module.handler(
        {
            "Records": [
                {
                    "s3": {
                        "bucket": {"name": "lmn-staging-private-context-logs-896586841071"},
                        "object": {"key": "access/example.log"},
                    }
                }
            ]
        },
        None,
    )

    assert result == {"processed_objects": 1, "unexpected_denials": 6}
    assert fake.cloudwatch.metric_data == [
        {
            "Namespace": "lmn/staging",
            "MetricData": [
                {
                    "MetricName": "private_context_unexpected_denials",
                    "Value": 6,
                    "Unit": "Count",
                }
            ],
        }
    ]
