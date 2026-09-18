from __future__ import annotations

import datetime as dt
import importlib.util
import sys
import types
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

HANDLER_PATH = (
    Path(__file__).resolve().parents[2] / "infra" / "terraform" / "bedrock-spend-cap" / "handler.py"
)
TODAY = dt.date(2026, 1, 8)
YESTERDAY = TODAY - dt.timedelta(days=1)
THRESHOLD = float(len("configured"))
WARN_THRESHOLD = float(len("warning"))


class FixedDateTime(dt.datetime):
    @classmethod
    def now(cls, tz: dt.tzinfo | None = None) -> dt.datetime:
        return cls(2026, 1, 8, 12, tzinfo=tz or dt.timezone.utc)


class FakePaginator:
    def paginate(self, **kwargs):
        return []


class FakeCloudWatch:
    def get_paginator(self, name: str) -> FakePaginator:
        assert name == "list_metrics"
        return FakePaginator()


class FakeSSM:
    class exceptions:
        class ParameterNotFound(Exception):
            pass

    def __init__(self, state: dict | None = None) -> None:
        self.state = state or {}

    def get_parameter(self, Name: str) -> dict:
        if not self.state:
            raise self.exceptions.ParameterNotFound
        return {"Parameter": {"Value": json_dumps(self.state)}}

    def put_parameter(self, Name: str, Value: str, Type: str, Overwrite: bool) -> None:
        assert Type == "String"
        assert Overwrite is True
        self.state = json_loads(Value)


class FakeCE:
    def get_cost_and_usage(self, **kwargs):
        return {"ResultsByTime": []}


class FakeSNS:
    def __init__(self) -> None:
        self.published: list[dict[str, str]] = []

    def publish(self, TopicArn: str, Subject: str, Message: str) -> None:
        self.published.append({"TopicArn": TopicArn, "Subject": Subject, "Message": Message})


class FakeIAM:
    class exceptions:
        class NoSuchEntityException(Exception):
            pass

    def __init__(self, fail_attach_roles: set[str] | None = None) -> None:
        self.role_policies: dict[str, set[str]] = {}
        self.group_policies: dict[str, set[str]] = {}
        self.fail_attach_roles = fail_attach_roles or set()

    def list_attached_role_policies(self, RoleName: str) -> dict:
        return {
            "AttachedPolicies": [
                {"PolicyArn": policy} for policy in self.role_policies.get(RoleName, set())
            ]
        }

    def list_attached_group_policies(self, GroupName: str) -> dict:
        return {
            "AttachedPolicies": [
                {"PolicyArn": policy} for policy in self.group_policies.get(GroupName, set())
            ]
        }

    def attach_role_policy(self, RoleName: str, PolicyArn: str) -> None:
        if RoleName in self.fail_attach_roles:
            raise client_error("AttachRolePolicy")
        self.role_policies.setdefault(RoleName, set()).add(PolicyArn)

    def detach_role_policy(self, RoleName: str, PolicyArn: str) -> None:
        self.role_policies.setdefault(RoleName, set()).discard(PolicyArn)

    def attach_group_policy(self, GroupName: str, PolicyArn: str) -> None:
        self.group_policies.setdefault(GroupName, set()).add(PolicyArn)

    def detach_group_policy(self, GroupName: str, PolicyArn: str) -> None:
        self.group_policies.setdefault(GroupName, set()).discard(PolicyArn)


class FakeAWS:
    def __init__(self, ssm: FakeSSM | None = None, iam: FakeIAM | None = None) -> None:
        self.ssm = ssm or FakeSSM()
        self.iam = iam or FakeIAM()
        self.ce = FakeCE()
        self.sns = FakeSNS()

    def client(self, service: str, **kwargs):
        if service == "iam":
            return self.iam
        if service == "ssm":
            return self.ssm
        if service == "ce":
            return self.ce
        if service == "cloudwatch":
            return FakeCloudWatch()
        if service == "sns":
            return self.sns
        raise AssertionError(service)


def json_dumps(value: dict) -> str:
    import json

    return json.dumps(value)


def json_loads(value: str) -> dict:
    import json

    return json.loads(value)


def client_error(operation: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "denied by test"}},
        operation,
    )


def load_handler(monkeypatch: pytest.MonkeyPatch, fake_aws: FakeAWS, *, dry_run=False):
    module_name = f"bedrock_spend_cap_under_test_{id(fake_aws)}"
    monkeypatch.setitem(
        sys.modules,
        "boto3",
        types.SimpleNamespace(client=fake_aws.client),
    )
    monkeypatch.setenv("THRESHOLD_USD", str(THRESHOLD))
    monkeypatch.setenv("WARN_THRESHOLD_USD", str(WARN_THRESHOLD))
    monkeypatch.setenv("DENY_POLICY_ARN", "arn:test:deny")
    monkeypatch.setenv("TARGET_ROLES", "api,worker")
    monkeypatch.setenv("TARGET_GROUPS", "admins")
    monkeypatch.setenv("STATE_PARAM", "/state")
    monkeypatch.setenv("SNS_TOPIC_ARN", "arn:test:sns")
    monkeypatch.setenv("REGIONS", "test-region")
    monkeypatch.setenv("UNKNOWN_RATE_PER_1K", str(float(len("fallback"))))
    monkeypatch.setenv("DRY_RUN", "true" if dry_run else "false")

    spec = importlib.util.spec_from_file_location(module_name, HANDLER_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.dt, "datetime", FixedDateTime)
    monkeypatch.setattr(module, "_refresh_rates", lambda: {})
    monkeypatch.setattr(module, "_ce_spend_today", lambda day: float(len("")))
    return module


def run_handler(module, spend: float) -> dict:
    module._metered_spend = lambda day_start, now, rates: spend
    return module.handler({}, None)


def assert_policy_attached(fake_aws: FakeAWS, *, attached: bool) -> None:
    expected = {"arn:test:deny"} if attached else set()
    assert fake_aws.iam.role_policies.get("api", set()) == expected
    assert fake_aws.iam.role_policies.get("worker", set()) == expected
    assert fake_aws.iam.group_policies.get("admins", set()) == expected


def test_trip_records_day_and_attaches_every_target(monkeypatch: pytest.MonkeyPatch):
    fake_aws = FakeAWS()
    module = load_handler(monkeypatch, fake_aws)

    result = run_handler(module, THRESHOLD + len("x"))

    assert result["deny_attached"] is True
    assert fake_aws.ssm.state["tripped_day"] == TODAY.isoformat()
    assert_policy_attached(fake_aws, attached=True)


def test_releases_on_later_utc_day_when_spend_is_back_under(
    monkeypatch: pytest.MonkeyPatch,
):
    fake_aws = FakeAWS(ssm=FakeSSM({"tripped_day": YESTERDAY.isoformat()}))
    module = load_handler(monkeypatch, fake_aws)
    fake_aws.iam.role_policies = {"api": {"arn:test:deny"}, "worker": {"arn:test:deny"}}
    fake_aws.iam.group_policies = {"admins": {"arn:test:deny"}}

    result = run_handler(module, THRESHOLD - len("x"))

    assert result["deny_attached"] is False
    assert_policy_attached(fake_aws, attached=False)


def test_releases_on_same_utc_day_when_spend_is_back_under(
    monkeypatch: pytest.MonkeyPatch,
):
    fake_aws = FakeAWS(ssm=FakeSSM({"tripped_day": TODAY.isoformat()}))
    module = load_handler(monkeypatch, fake_aws)
    fake_aws.iam.role_policies = {"api": {"arn:test:deny"}, "worker": {"arn:test:deny"}}
    fake_aws.iam.group_policies = {"admins": {"arn:test:deny"}}

    result = run_handler(module, THRESHOLD - len("x"))

    assert result["deny_attached"] is False
    assert_policy_attached(fake_aws, attached=False)


def test_dry_run_does_not_attach_or_poison_same_day_release(
    monkeypatch: pytest.MonkeyPatch,
):
    fake_aws = FakeAWS()
    module = load_handler(monkeypatch, fake_aws, dry_run=True)

    run_handler(module, THRESHOLD + len("x"))
    module.DRY_RUN = False
    fake_aws.iam.role_policies = {"api": {"arn:test:deny"}, "worker": {"arn:test:deny"}}
    fake_aws.iam.group_policies = {"admins": {"arn:test:deny"}}
    result = run_handler(module, THRESHOLD - len("x"))

    assert "tripped_day" not in fake_aws.ssm.state
    assert result["deny_attached"] is False
    assert_policy_attached(fake_aws, attached=False)


def test_partial_attach_failure_is_not_reported_as_fully_attached(
    monkeypatch: pytest.MonkeyPatch,
):
    fake_aws = FakeAWS(iam=FakeIAM(fail_attach_roles={"worker"}))
    module = load_handler(monkeypatch, fake_aws)

    with pytest.raises(RuntimeError, match="failed to attach"):
        run_handler(module, THRESHOLD + len("x"))

    assert fake_aws.iam.role_policies.get("api") == {"arn:test:deny"}
    assert fake_aws.iam.role_policies.get("worker", set()) == set()
    assert module._is_attached() is False


def test_warning_fires_at_warn_threshold_below_hard_cap(
    monkeypatch: pytest.MonkeyPatch,
):
    fake_aws = FakeAWS()
    module = load_handler(monkeypatch, fake_aws)

    result = run_handler(module, WARN_THRESHOLD)

    assert result["deny_attached"] is False
    assert fake_aws.ssm.state["warned_day"] == TODAY.isoformat()
    assert [m["Subject"] for m in fake_aws.sns.published] == ["Bedrock daily spend WARNING"]


def test_warning_does_not_fire_twice_in_same_utc_day(
    monkeypatch: pytest.MonkeyPatch,
):
    fake_aws = FakeAWS(ssm=FakeSSM({"warned_day": TODAY.isoformat()}))
    module = load_handler(monkeypatch, fake_aws)

    run_handler(module, WARN_THRESHOLD)

    assert fake_aws.sns.published == []


def test_warning_fires_again_on_new_utc_day(monkeypatch: pytest.MonkeyPatch):
    fake_aws = FakeAWS(ssm=FakeSSM({"warned_day": YESTERDAY.isoformat()}))
    module = load_handler(monkeypatch, fake_aws)

    run_handler(module, WARN_THRESHOLD)

    assert fake_aws.ssm.state["warned_day"] == TODAY.isoformat()
    assert len(fake_aws.sns.published) == len("x")


def test_warning_does_not_fire_below_warn_threshold(monkeypatch: pytest.MonkeyPatch):
    fake_aws = FakeAWS()
    module = load_handler(monkeypatch, fake_aws)

    run_handler(module, WARN_THRESHOLD - len("x"))

    assert "warned_day" not in fake_aws.ssm.state
    assert fake_aws.sns.published == []


def test_warning_does_not_shadow_hard_cap_trip(monkeypatch: pytest.MonkeyPatch):
    fake_aws = FakeAWS()
    module = load_handler(monkeypatch, fake_aws)

    run_handler(module, THRESHOLD)

    assert "warned_day" not in fake_aws.ssm.state
    assert fake_aws.ssm.state["tripped_day"] == TODAY.isoformat()
    assert_policy_attached(fake_aws, attached=True)


def test_partial_existing_attachment_reapplies_without_duplicate_trip_notification(
    monkeypatch: pytest.MonkeyPatch,
):
    fake_aws = FakeAWS()
    module = load_handler(monkeypatch, fake_aws)
    fake_aws.iam.role_policies = {"api": {"arn:test:deny"}}

    run_handler(module, THRESHOLD)

    assert fake_aws.ssm.state["tripped_day"] == TODAY.isoformat()
    assert_policy_attached(fake_aws, attached=True)
    assert fake_aws.sns.published == []


def test_dry_run_warning_matches_deployed_notify_only_behavior(
    monkeypatch: pytest.MonkeyPatch,
):
    fake_aws = FakeAWS()
    module = load_handler(monkeypatch, fake_aws, dry_run=True)

    result = run_handler(module, WARN_THRESHOLD)

    assert result["deny_attached"] is False
    assert fake_aws.ssm.state["warned_day"] == TODAY.isoformat()
    assert len(fake_aws.sns.published) == len("x")
    assert_policy_attached(fake_aws, attached=False)


def test_seed_for_missing_output_rate_falls_back_to_observed_input(
    monkeypatch: pytest.MonkeyPatch,
):
    module = load_handler(monkeypatch, FakeAWS())
    module.SEED_RATES = {"model": {"input": float(len("seed"))}}

    rate = module._rate_for("model", "OutputTokenCount", {"USE1-model-input-tokens": 2})

    assert rate == 2 * module.TOKEN_METRICS["OutputTokenCount"][1]


def test_rate_for_unpriced_model_uses_fail_closed_fallback(
    monkeypatch: pytest.MonkeyPatch,
):
    module = load_handler(monkeypatch, FakeAWS())
    module.SEED_RATES = {}

    rate = module._rate_for("unpriced-model", "OutputTokenCount", {})

    assert rate == module.UNKNOWN_RATE_PER_1K * module.TOKEN_METRICS["OutputTokenCount"][1]
