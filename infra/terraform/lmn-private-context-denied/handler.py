from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote_plus

import boto3


LOG_BUCKET = "lmn-staging-private-context-logs-896586841071"
LOG_PREFIX = "access/"
METRIC_NAMESPACE = "lmn/staging"
METRIC_NAME = "private_context_unexpected_denials"
AUDIT_SESSION_PREFIX = "lmn-staging-private-audit-"
JOE_FRONTIER = "arn:aws:iam::896586841071:user/joe_frontier"

s3 = boto3.client("s3")
cloudwatch = boto3.client("cloudwatch")


@dataclass(frozen=True)
class AccessLogRecord:
    requester: str
    status: str
    error_code: str


def _parse_access_log(line: str) -> AccessLogRecord | None:
    if not line.strip():
        return None
    try:
        fields = shlex.split(line)
    except ValueError:
        return None
    if len(fields) < 12:
        return None
    return AccessLogRecord(requester=fields[5], status=fields[10], error_code=fields[11])


def _is_audit_runner(requester: str) -> bool:
    parts = requester.split("/")
    return (
        len(parts) >= 3
        and parts[0].startswith("arn:aws:sts::896586841071:assumed-role")
        and parts[2].startswith(AUDIT_SESSION_PREFIX)
    )


def _is_unexpected_denial(record: AccessLogRecord) -> bool:
    return (
        record.status == "403"
        and record.error_code == "AccessDenied"
        and not _is_audit_runner(record.requester)
        and record.requester != JOE_FRONTIER
    )


def _count_unexpected_denials(body: str) -> int:
    return sum(
        1
        for line in body.splitlines()
        if (record := _parse_access_log(line)) is not None and _is_unexpected_denial(record)
    )


def _read_log_object(bucket: str, key: str) -> str:
    response = s3.get_object(Bucket=bucket, Key=key)
    return response["Body"].read().decode("utf-8", errors="replace")


def _put_metric(value: int) -> None:
    cloudwatch.put_metric_data(
        Namespace=METRIC_NAMESPACE,
        MetricData=[
            {
                "MetricName": METRIC_NAME,
                "Value": value,
                "Unit": "Count",
            }
        ],
    )


def handler(event: dict[str, Any], context: object | None = None) -> dict[str, int]:
    unexpected_denials = 0
    processed_objects = 0
    for record in event.get("Records", []):
        bucket = record.get("s3", {}).get("bucket", {}).get("name")
        key = unquote_plus(record.get("s3", {}).get("object", {}).get("key", ""))
        if bucket != LOG_BUCKET or not key.startswith(LOG_PREFIX):
            continue
        unexpected_denials += _count_unexpected_denials(_read_log_object(bucket, key))
        processed_objects += 1

    if processed_objects:
        _put_metric(unexpected_denials)
    return {"processed_objects": processed_objects, "unexpected_denials": unexpected_denials}
