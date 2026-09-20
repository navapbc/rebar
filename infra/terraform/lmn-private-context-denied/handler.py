from __future__ import annotations

import shlex
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import unquote_plus

import boto3

LOG_BUCKET = "lmn-staging-private-context-logs-896586841071"
LOG_PREFIX = "access/"
METRIC_NAMESPACE = "lmn/staging"
METRIC_NAME = "private_context_unexpected_denials"
AUDIT_SESSION_PREFIX = "lmn-staging-private-audit-"
JOE_FRONTIER = "arn:aws:iam::896586841071:user/joe_frontier"
SCAN_WINDOW = timedelta(minutes=5)

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


def _put_metric(value: int, *, timestamp: datetime | None = None) -> None:
    metric: dict[str, Any] = {
        "MetricName": METRIC_NAME,
        "Value": value,
        "Unit": "Count",
    }
    if timestamp is not None:
        metric["Timestamp"] = timestamp
    cloudwatch.put_metric_data(
        Namespace=METRIC_NAMESPACE,
        MetricData=[metric],
    )


def _event_time(event: dict[str, Any]) -> datetime:
    raw_time = event.get("time")
    if not isinstance(raw_time, str):
        return datetime.now(timezone.utc)
    return datetime.fromisoformat(raw_time.replace("Z", "+00:00"))


def _recent_log_keys(window_end: datetime) -> list[str]:
    window_start = window_end - SCAN_WINDOW
    keys: list[str] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=LOG_BUCKET, Prefix=LOG_PREFIX):
        for item in page.get("Contents", []):
            key = item.get("Key", "")
            last_modified = item.get("LastModified")
            if not isinstance(last_modified, datetime) or not key.startswith(LOG_PREFIX):
                continue
            if last_modified.tzinfo is None:
                last_modified = last_modified.replace(tzinfo=timezone.utc)
            if window_start < last_modified <= window_end:
                keys.append(key)
    return keys


def _count_event_records(event: dict[str, Any]) -> tuple[int, int]:
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
    return processed_objects, unexpected_denials


def _count_recent_logs(event: dict[str, Any]) -> tuple[int, int]:
    unexpected_denials = 0
    processed_objects = 0
    window_end = _event_time(event)
    for key in _recent_log_keys(window_end):
        unexpected_denials += _count_unexpected_denials(_read_log_object(LOG_BUCKET, key))
        processed_objects += 1
    _put_metric(unexpected_denials, timestamp=window_end)
    return processed_objects, unexpected_denials


def handler(event: dict[str, Any], context: object | None = None) -> dict[str, int]:
    if "Records" in event:
        processed_objects, unexpected_denials = _count_event_records(event)
    else:
        processed_objects, unexpected_denials = _count_recent_logs(event)
    return {"processed_objects": processed_objects, "unexpected_denials": unexpected_denials}
