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
    def __init__(self, pages: list[dict] | None = None, error: ClientError | None = None) -> None:
        self.pages = pages or []
        self.error = error

    def paginate(self, **kwargs):
        if self.error:
            raise self.error
        return self.pages


class FakeCloudWatch:
    def __init__(
        self,
        *,
        pages: list[dict] | None = None,
        values_by_id: dict[str, list[float]] | None = None,
        list_error: ClientError | None = None,
        data_error: ClientError | None = None,
    ) -> None:
        self.pages = pages or []
        self.values_by_id = values_by_id or {}
        self.list_error = list_error
        self.data_error = data_error

    def get_paginator(self, name: str) -> FakePaginator:
        assert name == "list_metrics"
        return FakePaginator(self.pages, self.list_error)

    def get_metric_data(self, MetricDataQueries: list[dict], **kwargs) -> dict:
        if self.data_error:
            raise self.data_error
        return {
            "MetricDataResults": [
                {"Id": query["Id"], "Values": self.values_by_id.get(query["Id"], [])}
                for query in MetricDataQueries
            ]
        }


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
    def __init__(
        self,
        spend: float | None = None,
        error: ClientError | None = None,
        results_by_time: list[dict] | None = None,
    ) -> None:
        self.spend = float(0) if spend is None else spend
        self.error = error
        self.results_by_time = results_by_time
        self.calls = 0

    def get_cost_and_usage(self, **kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        if self.results_by_time is not None:
            return {"ResultsByTime": self.results_by_time}
        return {
            "ResultsByTime": [
                {
                    "Total": {
                        "UnblendedCost": {"Amount": str(self.spend)},
                    }
                }
            ]
        }


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
    def __init__(
        self,
        ssm: FakeSSM | None = None,
        iam: FakeIAM | None = None,
        ce: FakeCE | None = None,
        cloudwatch: FakeCloudWatch | None = None,
    ) -> None:
        self.ssm = ssm or FakeSSM()
        self.iam = iam or FakeIAM()
        self.ce = ce or FakeCE()
        self.cloudwatch = cloudwatch or FakeCloudWatch()
        self.sns = FakeSNS()

    def client(self, service: str, **kwargs):
        if service == "iam":
            return self.iam
        if service == "ssm":
            return self.ssm
        if service == "ce":
            return self.ce
        if service == "cloudwatch":
            return self.cloudwatch
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


def load_handler(
    monkeypatch: pytest.MonkeyPatch,
    fake_aws: FakeAWS,
    *,
    dry_run=False,
    stub_ce=True,
    stub_rates=True,
):
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
    if stub_rates:
        monkeypatch.setattr(module, "_refresh_rates", lambda: {})
    if stub_ce:
        monkeypatch.setattr(module, "_ce_spend_today", lambda day: float(len("")))
    return module


def run_handler(module, spend: float) -> dict:
    module._metered_spend = lambda day_start, now, rates: spend
    return module.handler({}, None)


def run_handler_with_unavailable_meter(module) -> dict:
    module._metered_spend = lambda day_start, now, rates: None
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


def test_first_trip_publishes_tripped_notification(monkeypatch: pytest.MonkeyPatch):
    fake_aws = FakeAWS()
    module = load_handler(monkeypatch, fake_aws)

    run_handler(module, THRESHOLD + len("x"))

    assert [m["Subject"] for m in fake_aws.sns.published] == [
        f"Bedrock daily cap TRIPPED — {THRESHOLD + len('x'):,.2f} >= {THRESHOLD:,.2f}"
    ]


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


def test_release_publishes_cap_released_notification(monkeypatch: pytest.MonkeyPatch):
    fake_aws = FakeAWS(ssm=FakeSSM({"tripped_day": YESTERDAY.isoformat()}))
    module = load_handler(monkeypatch, fake_aws)
    fake_aws.iam.role_policies = {"api": {"arn:test:deny"}, "worker": {"arn:test:deny"}}
    fake_aws.iam.group_policies = {"admins": {"arn:test:deny"}}

    run_handler(module, WARN_THRESHOLD - len("x"))

    assert [m["Subject"] for m in fake_aws.sns.published] == ["Bedrock daily cap released"]


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


def test_metered_spend_returns_unavailable_when_cloudwatch_listing_fails(
    monkeypatch: pytest.MonkeyPatch,
):
    fake_aws = FakeAWS(cloudwatch=FakeCloudWatch(list_error=client_error("ListMetrics")))
    module = load_handler(monkeypatch, fake_aws)

    result = module._metered_spend(
        FixedDateTime.now(dt.timezone.utc), FixedDateTime.now(dt.timezone.utc), {}
    )

    assert result is None


def test_metered_spend_returns_unavailable_when_cloudwatch_data_fails(
    monkeypatch: pytest.MonkeyPatch,
):
    fake_aws = FakeAWS(
        cloudwatch=FakeCloudWatch(
            pages=[
                {
                    "Metrics": [
                        {
                            "MetricName": "InputTokenCount",
                            "Dimensions": [{"Name": "ModelId", "Value": "model"}],
                        }
                    ]
                }
            ],
            data_error=client_error("GetMetricData"),
        )
    )
    module = load_handler(monkeypatch, fake_aws)

    result = module._metered_spend(
        FixedDateTime.now(dt.timezone.utc), FixedDateTime.now(dt.timezone.utc), {}
    )

    assert result is None


def test_metered_spend_prices_known_and_unknown_model_tokens(
    monkeypatch: pytest.MonkeyPatch,
):
    known_units_1k = float(len("known"))
    unknown_units_1k = float(len("new"))
    known_rate = float(len("rate"))
    fake_aws = FakeAWS(
        cloudwatch=FakeCloudWatch(
            pages=[
                {
                    "Metrics": [
                        {
                            "MetricName": "InputTokenCount",
                            "Dimensions": [
                                {
                                    "Name": "ModelId",
                                    "Value": "us.anthropic.claude-sonnet-4-6",
                                }
                            ],
                        },
                        {
                            "MetricName": "OutputTokenCount",
                            "Dimensions": [{"Name": "ModelId", "Value": "unpriced-model"}],
                        },
                    ]
                }
            ],
            values_by_id={
                "q0": [known_units_1k * 1000],
                "q1": [unknown_units_1k * 1000],
            },
        )
    )
    module = load_handler(monkeypatch, fake_aws)
    module.SEED_RATES = {}

    result = module._metered_spend(
        FixedDateTime.now(dt.timezone.utc),
        FixedDateTime.now(dt.timezone.utc),
        {"USE1-claude-sonnet-4-6-input-tokens": known_rate},
    )

    assert result == (
        known_units_1k * known_rate
        + unknown_units_1k
        * module.UNKNOWN_RATE_PER_1K
        * module.TOKEN_METRICS["OutputTokenCount"][1]
    )


def test_unavailable_metered_signal_trips_unattached_cap(
    monkeypatch: pytest.MonkeyPatch,
):
    fake_aws = FakeAWS(ssm=FakeSSM({"ce_day": TODAY.isoformat(), "ce_spend": float(0)}))
    module = load_handler(monkeypatch, fake_aws)

    result = run_handler_with_unavailable_meter(module)

    assert result["metered_status"] == "unavailable"
    assert result["effective_source"] == "metered_unavailable"
    assert result["deny_attached"] is True


def test_unavailable_metered_signal_keeps_existing_restriction(
    monkeypatch: pytest.MonkeyPatch,
):
    fake_aws = FakeAWS(ssm=FakeSSM({"ce_day": TODAY.isoformat(), "ce_spend": float(0)}))
    module = load_handler(monkeypatch, fake_aws)
    fake_aws.iam.role_policies = {"api": {"arn:test:deny"}, "worker": {"arn:test:deny"}}
    fake_aws.iam.group_policies = {"admins": {"arn:test:deny"}}

    result = run_handler_with_unavailable_meter(module)

    assert result["metered_status"] == "unavailable"
    assert result["deny_attached"] is True
    assert_policy_attached(fake_aws, attached=True)


def test_quiet_day_without_bedrock_series_releases_existing_restriction(
    monkeypatch: pytest.MonkeyPatch,
):
    state = {
        "ce_day": TODAY.isoformat(),
        "ce_spend": float(0),
        "ce_last_poll": FixedDateTime.now(dt.timezone.utc).isoformat(),
    }
    fake_aws = FakeAWS(ssm=FakeSSM(state), cloudwatch=FakeCloudWatch(pages=[]))
    module = load_handler(monkeypatch, fake_aws, stub_ce=False)
    fake_aws.iam.role_policies = {"api": {"arn:test:deny"}, "worker": {"arn:test:deny"}}
    fake_aws.iam.group_policies = {"admins": {"arn:test:deny"}}

    result = module.handler({}, None)

    assert result["deny_attached"] is False


def test_unavailable_ce_signal_keeps_existing_restriction(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    fake_aws = FakeAWS(ce=FakeCE(error=client_error("GetCostAndUsage")))
    module = load_handler(monkeypatch, fake_aws, stub_ce=False)
    fake_aws.iam.role_policies = {"api": {"arn:test:deny"}, "worker": {"arn:test:deny"}}
    fake_aws.iam.group_policies = {"admins": {"arn:test:deny"}}

    result = run_handler(module, WARN_THRESHOLD - len("x"))

    assert result["ce_status"] == "unavailable"
    assert result["deny_attached"] is True
    assert_policy_attached(fake_aws, attached=True)
    assert "no cached" in caplog.text


def test_due_ce_failure_reuses_same_day_cached_signal(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    cached_spend = WARN_THRESHOLD
    state = {
        "ce_day": TODAY.isoformat(),
        "ce_spend": cached_spend,
        "ce_last_poll": (FixedDateTime.now(dt.timezone.utc) - dt.timedelta(hours=2)).isoformat(),
    }
    fake_aws = FakeAWS(ssm=FakeSSM(state), ce=FakeCE(error=client_error("GetCostAndUsage")))
    module = load_handler(monkeypatch, fake_aws, stub_ce=False)
    fake_aws.iam.role_policies = {"api": {"arn:test:deny"}, "worker": {"arn:test:deny"}}
    fake_aws.iam.group_policies = {"admins": {"arn:test:deny"}}

    result = run_handler(module, WARN_THRESHOLD - len("x"))

    assert fake_aws.ce.calls == len("x")
    assert result["ce_status"] == "cached"
    assert result["ce_usd"] == cached_spend
    assert result["effective_source"] == "ce"
    assert result["deny_attached"] is False
    assert_policy_attached(fake_aws, attached=False)
    assert "cached" in caplog.text


def test_due_ce_read_can_determine_effective_spend(
    monkeypatch: pytest.MonkeyPatch,
):
    fake_aws = FakeAWS(ce=FakeCE(spend=THRESHOLD - len("x")))
    module = load_handler(monkeypatch, fake_aws, stub_ce=False)

    result = run_handler(module, WARN_THRESHOLD - len("x"))

    assert fake_aws.ce.calls == len("x")
    assert result["effective_source"] == "ce"


def test_not_due_and_not_near_threshold_skips_ce_read(
    monkeypatch: pytest.MonkeyPatch,
):
    state = {
        "ce_day": TODAY.isoformat(),
        "ce_spend": float(0),
        "ce_last_poll": FixedDateTime.now(dt.timezone.utc).isoformat(),
    }
    fake_aws = FakeAWS(ssm=FakeSSM(state), ce=FakeCE(spend=THRESHOLD))
    module = load_handler(monkeypatch, fake_aws, stub_ce=False)

    result = run_handler(module, WARN_THRESHOLD)

    assert fake_aws.ce.calls == len("")
    assert result["effective_source"] == "metered"


def test_near_threshold_metered_signal_forces_ce_read(
    monkeypatch: pytest.MonkeyPatch,
):
    state = {
        "ce_day": TODAY.isoformat(),
        "ce_spend": float(0),
        "ce_last_poll": FixedDateTime.now(dt.timezone.utc).isoformat(),
    }
    fake_aws = FakeAWS(ssm=FakeSSM(state), ce=FakeCE(spend=THRESHOLD - len("x")))
    module = load_handler(monkeypatch, fake_aws, stub_ce=False)

    result = run_handler(module, THRESHOLD - len("xx"))

    assert fake_aws.ce.calls == len("x")
    assert result["effective_source"] == "ce"


def test_metered_signal_can_determine_effective_spend_when_it_exceeds_ce(
    monkeypatch: pytest.MonkeyPatch,
):
    fake_aws = FakeAWS(ce=FakeCE(spend=WARN_THRESHOLD))
    module = load_handler(monkeypatch, fake_aws, stub_ce=False)

    result = run_handler(module, THRESHOLD - len("x"))

    assert fake_aws.ce.calls == len("x")
    assert result["effective_source"] == "metered"


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


def test_refresh_rates_derives_rates_from_positive_cost_and_quantity(
    monkeypatch: pytest.MonkeyPatch,
):
    first_charge = float(len("charged"))
    second_charge = float(len("more"))
    first_quantity = float(len("units"))
    second_quantity = float(len("again"))
    output_charge = float(len("output"))
    output_quantity = float(len("quantity"))
    fake_aws = FakeAWS(
        ce=FakeCE(
            results_by_time=[
                {
                    "Groups": [
                        {
                            "Keys": ["USE1-model-input-tokens"],
                            "Metrics": {
                                "UnblendedCost": {"Amount": str(first_charge)},
                                "UsageQuantity": {"Amount": str(first_quantity)},
                            },
                        },
                        {
                            "Keys": ["USE1-model-output-tokens"],
                            "Metrics": {
                                "UnblendedCost": {"Amount": str(output_charge)},
                                "UsageQuantity": {"Amount": str(output_quantity)},
                            },
                        },
                        {
                            "Keys": ["USE1-zero-quantity-input-tokens"],
                            "Metrics": {
                                "UnblendedCost": {"Amount": str(float(len("guard")))},
                                "UsageQuantity": {"Amount": str(float(len("")))},
                            },
                        },
                    ]
                },
                {
                    "Groups": [
                        {
                            "Keys": ["USE1-model-input-tokens"],
                            "Metrics": {
                                "UnblendedCost": {"Amount": str(second_charge)},
                                "UsageQuantity": {"Amount": str(second_quantity)},
                            },
                        },
                        {
                            "Keys": ["USE1-zero-charge-input-tokens"],
                            "Metrics": {
                                "UnblendedCost": {"Amount": str(float(len("")))},
                                "UsageQuantity": {"Amount": str(float(len("guard")))},
                            },
                        },
                    ]
                },
            ]
        )
    )
    module = load_handler(monkeypatch, fake_aws, stub_rates=False)

    rates = module._refresh_rates()

    assert rates == {
        "USE1-model-input-tokens": (first_charge + second_charge)
        / (first_quantity + second_quantity),
        "USE1-model-output-tokens": output_charge / output_quantity,
    }


def test_normalise_strips_stacked_single_and_no_prefixes(monkeypatch: pytest.MonkeyPatch):
    module = load_handler(monkeypatch, FakeAWS())

    assert module._normalise("us.anthropic.claude-sonnet-4-6") == "claudesonnet46"
    assert module._normalise("anthropic.claude-sonnet-4-6") == "claudesonnet46"
    assert module._normalise("claude-sonnet-4-6") == "claudesonnet46"


def test_load_state_corrupt_parameter_starts_empty(monkeypatch: pytest.MonkeyPatch):
    class CorruptSSM(FakeSSM):
        def get_parameter(self, Name: str) -> dict:
            return {"Parameter": {"Value": "not-json"}}

    fake_aws = FakeAWS(ssm=CorruptSSM({"present": True}))
    module = load_handler(monkeypatch, fake_aws)

    result = run_handler(module, WARN_THRESHOLD - len("x"))

    assert result["deny_attached"] is False
    assert fake_aws.ssm.state["ce_day"] == TODAY.isoformat()


def test_seed_for_cache_token_factors_and_nonmatching_seed(monkeypatch: pytest.MonkeyPatch):
    module = load_handler(monkeypatch, FakeAWS())
    base = float(len("seed"))
    module.SEED_RATES = {
        "other-model": {"input": base * 2, "output": base * 3},
        "target-model": {"input": base, "output": base * 4},
    }

    read_rate = module._seed_for("target-model", "CacheReadInputTokenCount")
    write_rate = module._seed_for("target-model", "CacheWriteInputTokenCount")

    assert read_rate == base / 10
    assert write_rate == base * 5 / 4


def test_seed_for_returns_none_when_matching_seed_has_no_input_rate(
    monkeypatch: pytest.MonkeyPatch,
):
    module = load_handler(monkeypatch, FakeAWS())
    module.SEED_RATES = {"target-model": {"output": float(len("seed"))}}

    rate = module._seed_for("target-model", "InputTokenCount")

    assert rate is None


def test_rate_for_uses_override_pin_before_model_id(monkeypatch: pytest.MonkeyPatch):
    module = load_handler(monkeypatch, FakeAWS())
    module.RATE_OVERRIDES = {
        "other-model": "WrongFragment",
        "titan-embed-text-v2": "TitanEmbeddingV2-Text",
    }
    pinned_rate = float(len("pinned"))

    rate = module._rate_for(
        "amazon.titan-embed-text-v2:0",
        "InputTokenCount",
        {"USE1-TitanEmbeddingV2-Text-input-tokens": pinned_rate},
    )

    assert rate == pinned_rate


def test_rate_for_ignores_empty_usage_fragment_and_keeps_more_specific_match(
    monkeypatch: pytest.MonkeyPatch,
):
    module = load_handler(monkeypatch, FakeAWS())
    specific_rate = float(len("specific"))
    shorter_rate = float(len("short"))

    rate = module._rate_for(
        "super-model",
        "InputTokenCount",
        {
            "input-tokens": float(len("empty")),
            "USE1-super-model-input-tokens": specific_rate,
            "USE1-model-input-tokens": shorter_rate,
        },
    )

    assert rate == specific_rate


def test_rate_for_uses_seed_when_empirical_rates_do_not_match(
    monkeypatch: pytest.MonkeyPatch,
):
    module = load_handler(monkeypatch, FakeAWS())
    seed_rate = float(len("seed"))
    module.SEED_RATES = {"target-model": {"input": seed_rate}}

    rate = module._rate_for(
        "target-model",
        "InputTokenCount",
        {"USE1-other-model-input-tokens": seed_rate * 2},
    )

    assert rate == seed_rate


def test_metered_spend_ignores_non_token_and_non_model_series_then_reports_zero(
    monkeypatch: pytest.MonkeyPatch,
):
    fake_aws = FakeAWS(
        cloudwatch=FakeCloudWatch(
            pages=[
                {
                    "Metrics": [
                        {"MetricName": "InvocationCount", "Dimensions": []},
                        {
                            "MetricName": "InputTokenCount",
                            "Dimensions": [{"Name": "Operation", "Value": "InvokeModel"}],
                        },
                    ]
                }
            ]
        )
    )
    module = load_handler(monkeypatch, fake_aws)

    result = module._metered_spend(
        FixedDateTime.now(dt.timezone.utc), FixedDateTime.now(dt.timezone.utc), {}
    )

    assert result == float(0)


def test_metered_spend_skips_zero_value_series(monkeypatch: pytest.MonkeyPatch):
    rate = float(len("rate"))
    fake_aws = FakeAWS(
        cloudwatch=FakeCloudWatch(
            pages=[
                {
                    "Metrics": [
                        {
                            "MetricName": "InputTokenCount",
                            "Dimensions": [{"Name": "ModelId", "Value": "zero-model"}],
                        },
                        {
                            "MetricName": "InputTokenCount",
                            "Dimensions": [{"Name": "ModelId", "Value": "paid-model"}],
                        },
                    ]
                }
            ],
            values_by_id={"q0": [0], "q1": [1000]},
        )
    )
    module = load_handler(monkeypatch, fake_aws)

    result = module._metered_spend(
        FixedDateTime.now(dt.timezone.utc),
        FixedDateTime.now(dt.timezone.utc),
        {"USE1-paid-model-input-tokens": rate},
    )

    assert result == rate


def test_ce_spend_today_returns_zero_when_ce_has_no_results(
    monkeypatch: pytest.MonkeyPatch,
):
    module = load_handler(
        monkeypatch,
        FakeAWS(ce=FakeCE(results_by_time=[])),
        stub_ce=False,
    )

    spend = module._ce_spend_today(TODAY)

    assert spend == float(0)


def test_is_attached_returns_false_with_no_targets(monkeypatch: pytest.MonkeyPatch):
    module = load_handler(monkeypatch, FakeAWS())
    module.TARGET_ROLES = []
    module.TARGET_GROUPS = []

    assert module._is_attached() is False


def test_attachment_checks_treat_missing_targets_as_unattached(
    monkeypatch: pytest.MonkeyPatch,
):
    fake_aws = FakeAWS()
    module = load_handler(monkeypatch, fake_aws)

    def missing_role(RoleName: str) -> dict:
        raise fake_aws.iam.exceptions.NoSuchEntityException()

    monkeypatch.setattr(fake_aws.iam, "list_attached_role_policies", missing_role)
    assert module._is_attached() is False
    assert module._has_any_attachment() is False

    fake_aws = FakeAWS()
    module = load_handler(monkeypatch, fake_aws)

    def attached_role(RoleName: str) -> dict:
        return {"AttachedPolicies": [{"PolicyArn": module.DENY_POLICY_ARN}]}

    def missing_group(GroupName: str) -> dict:
        raise fake_aws.iam.exceptions.NoSuchEntityException()

    monkeypatch.setattr(fake_aws.iam, "list_attached_role_policies", attached_role)
    monkeypatch.setattr(fake_aws.iam, "list_attached_group_policies", missing_group)

    assert module._is_attached() is False

    fake_aws = FakeAWS()
    module = load_handler(monkeypatch, fake_aws)

    def unattached_role(RoleName: str) -> dict:
        return {"AttachedPolicies": [{"PolicyArn": module.DENY_POLICY_ARN}]}

    def unattached_group(GroupName: str) -> dict:
        return {"AttachedPolicies": []}

    monkeypatch.setattr(fake_aws.iam, "list_attached_role_policies", unattached_role)
    monkeypatch.setattr(fake_aws.iam, "list_attached_group_policies", unattached_group)

    assert module._is_attached() is False


def test_has_any_attachment_treats_missing_group_as_unattached(
    monkeypatch: pytest.MonkeyPatch,
):
    fake_aws = FakeAWS()
    module = load_handler(monkeypatch, fake_aws)

    def missing_group(GroupName: str) -> dict:
        raise fake_aws.iam.exceptions.NoSuchEntityException()

    monkeypatch.setattr(fake_aws.iam, "list_attached_group_policies", missing_group)

    assert module._has_any_attachment() is False


def test_has_any_attachment_finds_group_policy(monkeypatch: pytest.MonkeyPatch):
    fake_aws = FakeAWS()
    module = load_handler(monkeypatch, fake_aws)
    fake_aws.iam.group_policies = {"admins": {"arn:test:deny"}}

    assert module._has_any_attachment() is True


def test_apply_reports_group_failures(monkeypatch: pytest.MonkeyPatch):
    fake_aws = FakeAWS()
    module = load_handler(monkeypatch, fake_aws)

    def fail_group(GroupName: str, PolicyArn: str) -> None:
        raise client_error("AttachGroupPolicy")

    monkeypatch.setattr(fake_aws.iam, "attach_group_policy", fail_group)

    with pytest.raises(RuntimeError, match="group/admins"):
        module._apply(attach=True)

    assert fake_aws.iam.role_policies == {
        "api": {"arn:test:deny"},
        "worker": {"arn:test:deny"},
    }


def test_notify_noops_without_topic_client(monkeypatch: pytest.MonkeyPatch):
    module = load_handler(monkeypatch, FakeAWS())
    module.sns = None

    module._notify("subject", "message")

    assert module.sns is None


def test_notify_logs_publish_failures(monkeypatch: pytest.MonkeyPatch):
    fake_aws = FakeAWS()
    module = load_handler(monkeypatch, fake_aws)

    def fail_publish(**kwargs) -> None:
        raise client_error("Publish")

    monkeypatch.setattr(fake_aws.sns, "publish", fail_publish)

    module._notify("subject", "message")

    assert fake_aws.sns.published == []


def test_cached_rates_skip_refresh(monkeypatch: pytest.MonkeyPatch):
    state = {
        "rates_day": TODAY.isoformat(),
        "rates": {"USE1-model-input-tokens": float(len("rate"))},
        "ce_day": TODAY.isoformat(),
        "ce_spend": float(0),
        "ce_last_poll": FixedDateTime.now(dt.timezone.utc).isoformat(),
    }
    fake_aws = FakeAWS(ssm=FakeSSM(state))
    module = load_handler(monkeypatch, fake_aws, stub_rates=False, stub_ce=False)

    def fail_refresh() -> dict:
        raise AssertionError("cached rates should be reused")

    monkeypatch.setattr(module, "_refresh_rates", fail_refresh)

    result = run_handler(module, WARN_THRESHOLD - len("x"))

    assert result["effective_source"] == "metered"


def test_refresh_rate_failure_reuses_empty_rates(monkeypatch: pytest.MonkeyPatch):
    fake_aws = FakeAWS()
    module = load_handler(monkeypatch, fake_aws, stub_rates=False)

    def fail_refresh() -> dict:
        raise client_error("GetCostAndUsage")

    monkeypatch.setattr(module, "_refresh_rates", fail_refresh)

    result = run_handler(module, WARN_THRESHOLD - len("x"))

    assert result["effective_source"] == "metered"
    assert "rates_day" not in fake_aws.ssm.state


def test_unavailable_meter_uses_ce_when_ce_has_reached_cap(
    monkeypatch: pytest.MonkeyPatch,
):
    fake_aws = FakeAWS(ce=FakeCE(spend=THRESHOLD))
    module = load_handler(monkeypatch, fake_aws, stub_ce=False)

    result = run_handler_with_unavailable_meter(module)

    assert result["metered_status"] == "unavailable"
    assert result["effective_source"] == "ce"
    assert result["deny_attached"] is True
