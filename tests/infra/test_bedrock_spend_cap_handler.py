from __future__ import annotations

import ast
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
    def __init__(self, pages, error):
        self.pages = pages
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
        data_error: ClientError | None = None,
    ) -> None:
        self.pages = pages or []
        self.values_by_id = values_by_id or {}
        self.paginator_calls = 0
        self.data_error = data_error

    def get_paginator(self, name: str) -> FakePaginator:
        # Enumeration is legitimate on the ATTRIBUTION path and forbidden on the
        # enforcement path, so record the calls rather than refusing them and let the
        # tests assert which path made them.
        assert name == "list_metrics"
        self.paginator_calls += 1
        return FakePaginator(self.pages, None)

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

    def __init__(self, state: dict | None = None, *, guard_done: bool = True) -> None:
        self.state = state or {}
        # The upper-bound guard makes one extra Cost Explorer read, at most once per
        # UTC day. Default the harness to "already ran today" so CE-accounting tests
        # measure the enforcement path alone; the guard's own tests clear it.
        if guard_done and self.state:
            # The guard keys on the SETTLED day it processed, not on today.
            settled = TODAY - dt.timedelta(days=2)
            self.state.setdefault("guard_day", settled.isoformat())

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
        pages: list[dict] | None = None,
    ) -> None:
        self.spend = float(0) if spend is None else spend
        self.error = error
        self.results_by_time = results_by_time
        self.pages = pages
        self.calls = 0
        self.requests: list[dict] = []

    def get_cost_and_usage(self, **kwargs):
        self.calls += 1
        self.requests.append(kwargs)
        if self.error:
            raise self.error
        if self.pages is not None:
            return self.pages[self.calls - 1]
        if self.results_by_time is not None:
            return {"ResultsByTime": self.results_by_time}
        # The handler asks for every service grouped by SERVICE and classifies the
        # returned groups by predicate, so the default response is shaped the way the
        # real API shapes a grouped query: one Group per service, not a bare Total.
        # A non-Bedrock group is always included so every test that reads this signal
        # also proves the predicate excludes what it should.
        return {
            "ResultsByTime": [
                {
                    "Groups": [
                        {
                            "Keys": ["Claude Opus 4.8 (Amazon Bedrock Edition)"],
                            "Metrics": {"UnblendedCost": {"Amount": str(self.spend)}},
                        },
                        {
                            "Keys": ["Amazon Simple Storage Service"],
                            "Metrics": {"UnblendedCost": {"Amount": str(float(len("noise")))}},
                        },
                    ]
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
        cloudwatch_by_region: dict[str, FakeCloudWatch] | None = None,
    ) -> None:
        self.ssm = ssm or FakeSSM()
        self.iam = iam or FakeIAM()
        self.ce = ce or FakeCE()
        self.cloudwatch = cloudwatch or FakeCloudWatch()
        self.cloudwatch_by_region = cloudwatch_by_region or {}
        self.sns = FakeSNS()

    def client(self, service: str, **kwargs):
        if service == "iam":
            return self.iam
        if service == "ssm":
            return self.ssm
        if service == "ce":
            return self.ce
        if service == "cloudwatch":
            return self.cloudwatch_by_region.get(kwargs.get("region_name"), self.cloudwatch)
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


def token_metric_page(model_id: str = "model", metric_name: str = "InputTokenCount") -> dict:
    return {
        "Metrics": [
            {
                "MetricName": metric_name,
                "Dimensions": [{"Name": "ModelId", "Value": model_id}],
            }
        ]
    }


def degraded_metered_spend(module, total: float):
    failure = types.SimpleNamespace(region="degraded-region", exception_class="ClientError")
    spend_type = getattr(module, "MeteredSpend", None)
    if spend_type is None:
        return types.SimpleNamespace(total=total, degraded=True, failures=(failure,))
    return spend_type(
        total=total,
        degraded=True,
        failures=(
            module.MeteredFailure(
                region=failure.region,
                exception_class=failure.exception_class,
            ),
        ),
        saw_datapoints=True,
    )


def load_handler(
    monkeypatch: pytest.MonkeyPatch,
    fake_aws: FakeAWS,
    *,
    dry_run=False,
    stub_ce=True,
    regions: tuple[str, ...] = ("test-region",),
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
    monkeypatch.setenv("REGIONS", ",".join(regions))
    monkeypatch.setenv("DRY_RUN", "true" if dry_run else "false")

    spec = importlib.util.spec_from_file_location(module_name, HANDLER_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.dt, "datetime", FixedDateTime)
    if stub_ce:
        monkeypatch.setattr(module, "_ce_spend_today", lambda day: float(len("")))
    return module


def run_handler(module, spend: float, *, saw_datapoints: bool = True) -> dict:
    module._metered_spend = lambda day_start, now: module.MeteredSpend(
        total=spend,
        degraded=False,
        failures=(),
        saw_datapoints=saw_datapoints,
    )
    return module.handler({}, None)


def run_handler_with_unavailable_meter(module) -> dict:
    module._metered_spend = lambda day_start, now: None
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

    # An all-empty CloudWatch read is ambiguous on the FIRST tick of a day: it is both
    # a genuinely quiet account and a metric-delivery gap, and nothing persisted yet
    # says which. With a deny attached the handler holds, then releases on the next
    # tick once the high-water key exists and is still zero. A one-tick delay to the
    # automatic release is the deliberate price of never lifting a deny on a gap.
    first = module.handler({}, None)
    assert first["deny_attached"] is True

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

    # Two reads: the enforcement poll, plus the upper-bound guard's one-per-day
    # read of a settled day. This test starts from empty state, so the guard
    # has not yet run for that day.
    assert fake_aws.ce.calls == len("xx")
    assert result["effective_source"] == "ce"


def test_degraded_partial_meter_below_near_threshold_respects_ce_interval(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    partial = THRESHOLD * 0.7
    state = {
        "ce_day": TODAY.isoformat(),
        "ce_spend": float(0),
        "ce_last_poll": FixedDateTime.now(dt.timezone.utc).isoformat(),
    }
    fake_aws = FakeAWS(ssm=FakeSSM(state), ce=FakeCE(spend=THRESHOLD))
    module = load_handler(monkeypatch, fake_aws, stub_ce=False)
    module._metered_spend = lambda day_start, now: degraded_metered_spend(module, partial)

    result = module.handler({}, None)

    assert fake_aws.ce.calls == len("")
    assert result["metered_usd"] == round(partial, 4)
    assert result["effective_source"] == "metered"
    assert f"metered={partial:.4f}" in caplog.text


def test_degraded_partial_meter_at_near_threshold_polls_ce(
    monkeypatch: pytest.MonkeyPatch,
):
    partial = THRESHOLD * 0.8
    state = {
        "ce_day": TODAY.isoformat(),
        "ce_spend": float(0),
        "ce_last_poll": FixedDateTime.now(dt.timezone.utc).isoformat(),
    }
    fake_aws = FakeAWS(ssm=FakeSSM(state), ce=FakeCE(spend=float(0)))
    module = load_handler(monkeypatch, fake_aws, stub_ce=False)
    module._metered_spend = lambda day_start, now: degraded_metered_spend(module, partial)

    result = module.handler({}, None)

    assert fake_aws.ce.calls == len("x")
    assert result["metered_usd"] == round(partial, 4)
    assert result["effective_source"] == "metered"


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


def test_degraded_partial_meter_can_attach(monkeypatch: pytest.MonkeyPatch):
    state = {
        "ce_day": TODAY.isoformat(),
        "ce_spend": float(0),
        "ce_last_poll": FixedDateTime.now(dt.timezone.utc).isoformat(),
    }
    fake_aws = FakeAWS(ssm=FakeSSM(state), ce=FakeCE(spend=float(0)))
    module = load_handler(monkeypatch, fake_aws, stub_ce=False)
    module._metered_spend = lambda day_start, now: degraded_metered_spend(module, THRESHOLD)

    result = module.handler({}, None)

    assert fake_aws.ce.calls == len("x")
    assert result["deny_attached"] is True
    assert_policy_attached(fake_aws, attached=True)


def test_degraded_partial_meter_cannot_release(monkeypatch: pytest.MonkeyPatch):
    state = {
        "ce_day": TODAY.isoformat(),
        "ce_spend": float(0),
        "ce_last_poll": FixedDateTime.now(dt.timezone.utc).isoformat(),
    }
    fake_aws = FakeAWS(ssm=FakeSSM(state), ce=FakeCE(spend=float(0)))
    module = load_handler(monkeypatch, fake_aws, stub_ce=False)
    fake_aws.iam.role_policies = {"api": {"arn:test:deny"}, "worker": {"arn:test:deny"}}
    fake_aws.iam.group_policies = {"admins": {"arn:test:deny"}}
    module._metered_spend = lambda day_start, now: degraded_metered_spend(
        module, THRESHOLD - len("x")
    )

    result = module.handler({}, None)

    assert result["deny_attached"] is True
    assert_policy_attached(fake_aws, attached=True)
    assert fake_aws.sns.published == []


def test_metered_signal_can_determine_effective_spend_when_it_exceeds_ce(
    monkeypatch: pytest.MonkeyPatch,
):
    fake_aws = FakeAWS(ce=FakeCE(spend=WARN_THRESHOLD))
    module = load_handler(monkeypatch, fake_aws, stub_ce=False)

    result = run_handler(module, THRESHOLD - len("x"))

    # Two reads: the enforcement poll, plus the upper-bound guard's one-per-day
    # read of a settled day. This test starts from empty state, so the guard
    # has not yet run for that day.
    assert fake_aws.ce.calls == len("xx")
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


def test_load_state_corrupt_parameter_starts_empty(monkeypatch: pytest.MonkeyPatch):
    class CorruptSSM(FakeSSM):
        def get_parameter(self, Name: str) -> dict:
            return {"Parameter": {"Value": "not-json"}}

    fake_aws = FakeAWS(ssm=CorruptSSM({"present": True}))
    module = load_handler(monkeypatch, fake_aws)

    result = run_handler(module, WARN_THRESHOLD - len("x"))

    assert result["deny_attached"] is False
    assert fake_aws.ssm.state["ce_day"] == TODAY.isoformat()


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


def test_ce_spend_today_reads_all_cost_explorer_pages(
    monkeypatch: pytest.MonkeyPatch,
):
    fake_ce = FakeCE(
        pages=[
            {
                "ResultsByTime": [
                    {
                        "Groups": [
                            {
                                "Keys": ["Amazon Bedrock"],
                                "Metrics": {"UnblendedCost": {"Amount": "3"}},
                            }
                        ]
                    },
                ],
                "NextPageToken": "next-page",
            },
            {
                "ResultsByTime": [
                    {
                        "Groups": [
                            {
                                "Keys": ["Claude Opus 4.8 (Amazon Bedrock Edition)"],
                                "Metrics": {"UnblendedCost": {"Amount": "5"}},
                            }
                        ]
                    },
                ],
            },
        ]
    )
    module = load_handler(monkeypatch, FakeAWS(ce=fake_ce), stub_ce=False)

    spend = module._ce_spend_today(TODAY)

    assert spend == 8
    assert fake_ce.calls == 2
    assert "NextPageToken" not in fake_ce.requests[0]
    assert fake_ce.requests[1]["NextPageToken"] == "next-page"


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


def test_unavailable_meter_uses_ce_when_ce_has_reached_cap(
    monkeypatch: pytest.MonkeyPatch,
):
    fake_aws = FakeAWS(ce=FakeCE(spend=THRESHOLD))
    module = load_handler(monkeypatch, fake_aws, stub_ce=False)

    result = run_handler_with_unavailable_meter(module)

    assert result["metered_status"] == "unavailable"
    assert result["effective_source"] == "ce"
    assert result["deny_attached"] is True


def test_ce_spend_today_empty_total_returns_zero(
    monkeypatch: pytest.MonkeyPatch,
):
    module = load_handler(
        monkeypatch,
        FakeAWS(ce=FakeCE(results_by_time=[{"Total": {}}])),
        stub_ce=False,
    )

    spend = module._ce_spend_today(TODAY)

    assert spend == float(0)


def test_unavailable_meter_respects_ce_min_interval(
    monkeypatch: pytest.MonkeyPatch,
):
    state = {
        "ce_day": TODAY.isoformat(),
        "ce_spend": float(0),
        "ce_last_poll": FixedDateTime.now(dt.timezone.utc).isoformat(),
    }
    fake_aws = FakeAWS(ssm=FakeSSM(state), ce=FakeCE(spend=float(0)))
    module = load_handler(monkeypatch, fake_aws, stub_ce=False)

    run_handler_with_unavailable_meter(module)
    result = run_handler_with_unavailable_meter(module)

    assert result["metered_status"] == "unavailable"
    assert fake_aws.ce.calls == len("")


def test_unavailable_meter_polls_ce_after_interval(
    monkeypatch: pytest.MonkeyPatch,
):
    state = {
        "ce_day": TODAY.isoformat(),
        "ce_spend": float(0),
        "ce_last_poll": (
            FixedDateTime.now(dt.timezone.utc) - dt.timedelta(hours=len("xx"))
        ).isoformat(),
    }
    fake_aws = FakeAWS(ssm=FakeSSM(state), ce=FakeCE(spend=float(0)))
    module = load_handler(monkeypatch, fake_aws, stub_ce=False)

    result = run_handler_with_unavailable_meter(module)

    assert result["metered_status"] == "unavailable"
    assert result["ce_status"] == "available"
    assert fake_aws.ce.calls == len("x")


def test_dry_run_release_does_not_publish_detach_notification(
    monkeypatch: pytest.MonkeyPatch,
):
    fake_aws = FakeAWS()
    module = load_handler(monkeypatch, fake_aws, dry_run=True)
    fake_aws.iam.role_policies = {"api": {"arn:test:deny"}, "worker": {"arn:test:deny"}}
    fake_aws.iam.group_policies = {"admins": {"arn:test:deny"}}

    result = run_handler(module, THRESHOLD - len("x"))

    assert result["deny_attached"] is True
    assert_policy_attached(fake_aws, attached=True)
    subjects = [published["Subject"] for published in fake_aws.sns.published]
    assert "Bedrock daily cap released" not in subjects


# ---------------------------------------------------------------------------
# Ceiling pricing: the removals, the query shape, and the upper-bound property.
#
# These replace the per-model rate-table tests. The previous suite could not
# express the defect class that produced four live mispricings, because its
# fixtures built rate keys from the ModelId spelling rather than the billing
# label, so a label/id mismatch was unrepresentable. Pricing no longer consults
# either, which is what makes the class unreachable.
# ---------------------------------------------------------------------------
REMOVED_PRICING_SYMBOLS = (
    "_refresh_rates",
    "_rate_for",
    "_seed_for",
    "_normalise",
    "_strip_billing_region",
    "_marketplace_rate_key",
    "_rate_key_for_ce_group",
    "SEED_RATES",
    "RATE_OVERRIDES",
    "BEDROCK_SERVICE_VALUES",
    "MARKETPLACE_TOKEN_SUFFIXES",
    "UNKNOWN_RATE_PER_1K",
    "MARKETPLACE_TOKENS_PER_RATE_UNIT",
    "MeteredRate",
    "RateSource",
    "TOKEN_METRICS",
)


def test_pricing_derivation_layer_is_absent(monkeypatch: pytest.MonkeyPatch):
    module = load_handler(monkeypatch, FakeAWS())

    surviving = [name for name in REMOVED_PRICING_SYMBOLS if hasattr(module, name)]

    assert surviving == [], f"pricing-derivation symbols still present: {surviving}"


def test_module_docstring_no_longer_describes_a_derived_rate_table():
    source = HANDLER_PATH.read_text(encoding="utf-8")
    docstring = ast.get_docstring(ast.parse(source)) or ""

    # Stale prose outlives stale code and is how the next reader is misled. Scoped to
    # the docstring deliberately: the handler body still NAMES the retired state keys,
    # in the comment explaining that it ignores them on the first tick after deploy.
    # That comment is load-bearing, so a whole-file ban would be the wrong assertion.
    # Match the retired HEADINGS and affirmative claims, not bare phrases: the new
    # docstring legitimately says there is deliberately NO derived rate table, and a
    # naive substring ban would forbid saying so.
    for gone in (
        "WHERE THE PRICES COME FROM",
        "UNKNOWN_RATE_PER_1K",
        "seed rate",
        "Not a hardcoded table",
        "cost/quantity is the exact blended",
        "the rate table is refreshed",
    ):
        assert gone not in docstring, f"module docstring still describes {gone!r}"
    assert "CEILING" in docstring, "docstring should explain the ceiling it now uses"

    # Imports orphaned by the removals.
    assert "\nimport re\n" not in source
    assert "from typing import Literal" not in source
    # The retired env vars must not be read anywhere in the module.
    for gone in ("UNKNOWN_RATE_PER_1K", "SEED_RATES", "RATE_OVERRIDES", "RATE_WINDOW_DAYS"):
        assert f'os.environ["{gone}"]' not in source
        assert f'os.environ.get("{gone}"' not in source


def test_metered_spend_issues_four_undimensioned_queries_per_region(
    monkeypatch: pytest.MonkeyPatch,
):
    recorded: list[dict] = []

    class RecordingCloudWatch(FakeCloudWatch):
        def get_metric_data(self, MetricDataQueries, **kwargs):
            recorded.append({"queries": MetricDataQueries})
            return super().get_metric_data(MetricDataQueries, **kwargs)

    regions = ("region-one", "region-two")
    fake_aws = FakeAWS(cloudwatch_by_region={region: RecordingCloudWatch() for region in regions})
    module = load_handler(monkeypatch, fake_aws, regions=regions)

    module._metered_spend(FixedDateTime.now(dt.timezone.utc), FixedDateTime.now(dt.timezone.utc))

    assert len(recorded) == len(regions), "expected exactly one batched call per region"
    for call in recorded:
        assert len(call["queries"]) == 4
        for query in call["queries"]:
            metric = query["MetricStat"]["Metric"]
            assert metric["Namespace"] == "AWS/Bedrock"
            # An EXPLICIT empty list, not an absent key. Omitting Dimensions asks
            # CloudWatch for something else, so the distinction is load-bearing.
            assert metric["Dimensions"] == []
            assert query["MetricStat"]["Stat"] == "Sum"
        assert {q["MetricStat"]["Metric"]["MetricName"] for q in call["queries"]} == set(
            module.CEILING
        )


def test_ceiling_covers_every_observed_rate_in_the_billing_payload(
    monkeypatch: pytest.MonkeyPatch,
):
    """CEILING must sit at or above the highest rate the account is actually billed.

    The upper-bound property is the whole safety argument: if any real rate exceeds
    its ceiling the metered arm under-reads and the cap fires late. Rates are derived
    here the way the account bills them -- marketplace services meter in units of
    1,000,000 tokens, the legacy service in units of 1,000.
    """
    module = load_handler(monkeypatch, FakeAWS())
    observed = {
        "InputTokenCount": [
            ("Claude Opus 4.8 (Amazon Bedrock Edition)", 5.50, 1e6),
            ("Amazon Bedrock", 0.0008, 1e3),
        ],
        "OutputTokenCount": [("Claude Opus 4.7 (Amazon Bedrock Edition)", 27.50, 1e6)],
        "CacheReadInputTokenCount": [("Claude Opus 4.5 (Amazon Bedrock Edition)", 0.55, 1e6)],
        "CacheWriteInputTokenCount": [("Claude Opus 4.8 (Amazon Bedrock Edition)", 6.875, 1e6)],
    }

    for metric, rows in observed.items():
        for service, per_million, _unit in rows:
            per_token = per_million / 1e6
            assert module.CEILING[metric] >= per_token, (
                f"{service} bills {metric} above its ceiling"
            )


def test_is_bedrock_service_predicate_matches_every_live_service(
    monkeypatch: pytest.MonkeyPatch,
):
    module = load_handler(monkeypatch, FakeAWS())
    # The five the retired allowlist omitted are marked; each cost real money while
    # the authoritative signal could not see it.
    live = [
        "Amazon Bedrock",
        "Claude Sonnet 4.5 (Amazon Bedrock Edition)",
        "Claude Sonnet 4.6 (Amazon Bedrock Edition)",
        "Claude Haiku 4.5 (Amazon Bedrock Edition)",
        "Claude Opus 4.7 (Amazon Bedrock Edition)",
        "Claude Opus 4.8 (Amazon Bedrock Edition)",  # omitted
        "Claude Opus 5 (Amazon Bedrock Edition)",  # omitted
        "Claude Opus 4.5 (Amazon Bedrock Edition)",  # omitted
        "Cohere Rerank v3.5 (Amazon Bedrock Edition)",  # omitted
        "Claude 3 Haiku (Amazon Bedrock Edition)",  # omitted
    ]
    for service in live:
        assert module.is_bedrock_service(service), service
    for other in [
        "Amazon Simple Storage Service",
        "AWS Lambda",
        "Tax",  # billed separately, never attributed to a Bedrock service
        "Amazon Bedrock Guardrails X",
    ]:
        assert not module.is_bedrock_service(other), other


def test_ce_spend_today_sums_only_bedrock_groups(monkeypatch: pytest.MonkeyPatch):
    fake_ce = FakeCE(
        results_by_time=[
            {
                "Groups": [
                    {
                        "Keys": ["Claude Opus 4.8 (Amazon Bedrock Edition)"],
                        "Metrics": {"UnblendedCost": {"Amount": "7"}},
                    },
                    {
                        "Keys": ["Amazon Bedrock"],
                        "Metrics": {"UnblendedCost": {"Amount": "2"}},
                    },
                    {
                        "Keys": ["Amazon Simple Storage Service"],
                        "Metrics": {"UnblendedCost": {"Amount": "1000"}},
                    },
                ]
            }
        ]
    )
    module = load_handler(monkeypatch, FakeAWS(ce=fake_ce), stub_ce=False)

    spend = module._ce_spend_today(TODAY)

    assert spend == float(9)
    request = fake_ce.requests[0]
    # No SERVICE filter: an enumerated value list is exactly what went stale.
    assert "Filter" not in request
    assert request["GroupBy"] == [{"Type": "DIMENSION", "Key": "SERVICE"}]


# ---------------------------------------------------------------------------
# Release dispositions.
#
# An all-empty CloudWatch read is ambiguous: it is BOTH a metric-delivery gap and
# the normal idle / fresh-UTC-day state the automatic release depends on. Cost
# Explorer cannot break the tie -- it lags 8-24h and reads zero for hours into a
# busy day, so "CE says zero" would lift a legitimately tripped deny at noon. The
# day's high-water token total, keyed by UTC date, is what discriminates.
# ---------------------------------------------------------------------------
def _attach_deny(fake_aws) -> None:
    fake_aws.iam.role_policies = {"api": {"arn:test:deny"}, "worker": {"arn:test:deny"}}
    fake_aws.iam.group_policies = {"admins": {"arn:test:deny"}}


def _empty_read(module):
    module._metered_spend = lambda day_start, now: module.MeteredSpend(
        total=float(0), degraded=False, failures=(), saw_datapoints=False
    )


def test_empty_read_with_zero_high_water_today_releases(monkeypatch: pytest.MonkeyPatch):
    state = {
        "ce_day": TODAY.isoformat(),
        "ce_spend": float(0),
        "ce_last_poll": FixedDateTime.now(dt.timezone.utc).isoformat(),
        "high_water_day": TODAY.isoformat(),
        "tokens_high_water": float(0),
    }
    fake_aws = FakeAWS(ssm=FakeSSM(state))
    module = load_handler(monkeypatch, fake_aws)
    _attach_deny(fake_aws)
    _empty_read(module)

    result = module.handler({}, None)

    assert result["deny_attached"] is False


def test_empty_read_after_traffic_today_holds_the_deny(monkeypatch: pytest.MonkeyPatch):
    state = {
        "ce_day": TODAY.isoformat(),
        "ce_spend": float(0),
        "ce_last_poll": FixedDateTime.now(dt.timezone.utc).isoformat(),
        "high_water_day": TODAY.isoformat(),
        "tokens_high_water": float(len("seen traffic earlier today")),
    }
    fake_aws = FakeAWS(ssm=FakeSSM(state))
    module = load_handler(monkeypatch, fake_aws)
    _attach_deny(fake_aws)
    _empty_read(module)

    result = module.handler({}, None)

    # Tokens were observed earlier today, so an empty read now is a gap, not idleness.
    assert result["deny_attached"] is True


def test_empty_read_with_absent_high_water_holds_the_deny(monkeypatch: pytest.MonkeyPatch):
    """Mid-day deploy over state written by the previous handler.

    Pre-change state carries no high-water key at all. Treating "absent" as "idle"
    would release a legitimately tripped deny on the first tick after deploy, so
    absent holds while a deny is attached.
    """
    state = {
        "ce_day": TODAY.isoformat(),
        "ce_spend": float(0),
        "ce_last_poll": FixedDateTime.now(dt.timezone.utc).isoformat(),
        "rates": {"USE1-legacy-input-tokens": 0.003},
        "rates_day": TODAY.isoformat(),
    }
    fake_aws = FakeAWS(ssm=FakeSSM(state))
    module = load_handler(monkeypatch, fake_aws)
    _attach_deny(fake_aws)
    _empty_read(module)

    result = module.handler({}, None)

    assert result["deny_attached"] is True


def test_first_tick_after_deploy_ignores_and_drops_old_rate_state(
    monkeypatch: pytest.MonkeyPatch,
):
    state = {
        "ce_day": TODAY.isoformat(),
        "ce_spend": float(0),
        "ce_last_poll": FixedDateTime.now(dt.timezone.utc).isoformat(),
        "rates": {"USE1-legacy-input-tokens": 0.003},
        "rates_day": TODAY.isoformat(),
        "metered_estimated": True,
    }
    fake_aws = FakeAWS(ssm=FakeSSM(state))
    module = load_handler(monkeypatch, fake_aws)

    result = run_handler(module, float(1))

    assert "metered_estimated" not in result
    persisted = fake_aws.ssm.state
    for gone in ("rates", "rates_day", "metered_estimated"):
        assert gone not in persisted, f"{gone} survived into persisted state"


def test_partial_region_failure_holds_the_deny(monkeypatch: pytest.MonkeyPatch):
    """One region raised, the other returned data below threshold.

    The surviving region's total is a FLOOR on account spend, not the account total,
    so it cannot justify lifting a deny.
    """
    state = {
        "ce_day": TODAY.isoformat(),
        "ce_spend": float(0),
        "ce_last_poll": FixedDateTime.now(dt.timezone.utc).isoformat(),
        "high_water_day": TODAY.isoformat(),
        "tokens_high_water": float(0),
    }
    fake_aws = FakeAWS(ssm=FakeSSM(state))
    module = load_handler(monkeypatch, fake_aws)
    _attach_deny(fake_aws)
    module._metered_spend = lambda day_start, now: degraded_metered_spend(module, float(1))

    result = module.handler({}, None)

    assert result["deny_attached"] is True


@pytest.mark.parametrize("failing", ["metered", "ce"])
def test_read_exception_fails_closed(monkeypatch: pytest.MonkeyPatch, failing: str):
    """An exception is not an empty read, and must never release."""
    state = {
        "high_water_day": TODAY.isoformat(),
        "tokens_high_water": float(0),
    }
    if failing == "ce":
        fake_aws = FakeAWS(ssm=FakeSSM(state), ce=FakeCE(error=client_error("GetCostAndUsage")))
    else:
        fake_aws = FakeAWS(ssm=FakeSSM(state))
    module = load_handler(monkeypatch, fake_aws, stub_ce=(failing != "ce"))
    _attach_deny(fake_aws)
    if failing == "metered":
        module._metered_spend = lambda day_start, now: None

    result = module.handler({}, None)

    assert result["deny_attached"] is True


def test_ce_arm_trips_at_the_unscaled_threshold(monkeypatch: pytest.MonkeyPatch):
    """The authoritative arm must not be scaled by the ceiling's over-estimate.

    ce_spend is real billed dollars. Raising THRESHOLD_USD to centre the metered arm
    would raise the bar for this arm too, so the authoritative signal would fire LATE
    -- the failure this design exists to remove.
    """
    fake_aws = FakeAWS(ce=FakeCE(spend=THRESHOLD))
    module = load_handler(monkeypatch, fake_aws, stub_ce=False)
    # Metered reads clean and far below the cap; only the CE arm is at the threshold.
    module._metered_spend = lambda day_start, now: module.MeteredSpend(
        total=float(1), degraded=False, failures=(), saw_datapoints=True
    )

    result = module.handler({}, None)

    assert result["effective_source"] == "ce"
    assert result["effective_usd"] == pytest.approx(THRESHOLD)
    assert result["deny_attached"] is True


# ---------------------------------------------------------------------------
# Recorded-payload fixtures.
#
# The previous suite synthesized rate keys from the ModelId spelling rather than
# the real billing label, so a label/id mismatch was unrepresentable and four live
# mispricings coexisted with a green suite. These fixtures carry the STRUCTURE of
# real responses -- every usage-type convention AWS actually bills, in one file --
# with synthetic amounts, because this repository is public.
# ---------------------------------------------------------------------------
FIXTURES = Path(__file__).parent / "fixtures"
CE_FIXTURE = json_loads((FIXTURES / "bedrock_ce_payload.json").read_text(encoding="utf-8"))
LIST_METRICS_FIXTURE = json_loads(
    (FIXTURES / "bedrock_list_metrics.json").read_text(encoding="utf-8")
)


def _fixture_usage_types() -> list[tuple[str, str]]:
    return [
        (group["Keys"][0], group["Keys"][1])
        for period in CE_FIXTURE["ResultsByTime"]
        for group in period["Groups"]
    ]


def test_ce_fixture_carries_every_billing_convention():
    """One file must contain all four shapes, or the corpus silently loses coverage."""
    usage_types = [usage for _service, usage in _fixture_usage_types()]
    assert any("TokenCount" in u for u in usage_types), "CamelCase convention missing"
    assert any("_tokens_" in u for u in usage_types), "snake_case convention missing"
    assert any("lobal" in u for u in usage_types), "_Global variant missing"
    assert any("token" not in u.lower() for u in usage_types), "non-token usage type missing"


def test_ce_fixture_amounts_are_synthetic():
    """The repo is public, so no fixture may carry a real billed amount.

    Recorded amounts are replaced with an arithmetic progression, which is both
    obviously synthetic and cheap to assert.
    """
    for period in CE_FIXTURE["ResultsByTime"]:
        for index, group in enumerate(period["Groups"]):
            cost = float(group["Metrics"]["UnblendedCost"]["Amount"])
            quantity = float(group["Metrics"]["UsageQuantity"]["Amount"])
            assert cost == pytest.approx((index + 1) * 11)
            assert quantity == pytest.approx((index + 1) * 2)


def test_ce_fixture_includes_the_services_the_retired_allowlist_omitted(
    monkeypatch: pytest.MonkeyPatch,
):
    module = load_handler(monkeypatch, FakeAWS())
    services = {service for service, _usage in _fixture_usage_types()}
    omitted = [s for s in services if "Opus 4.8" in s or "Opus 5" in s or "Rerank" in s]
    assert omitted, "fixture should carry services the old allowlist could not see"
    for service in omitted:
        assert module.is_bedrock_service(service)


def test_every_recorded_model_is_priced_by_the_ceiling(monkeypatch: pytest.MonkeyPatch):
    """No ModelId in the recorded corpus may fall through to a fallback.

    There is no fallback path left -- pricing is a dict lookup on the metric name --
    so this asserts the recorded token metrics are exactly the four CEILING covers,
    and that a non-token metric such as SearchUnits is knowingly excluded rather than
    silently priced.
    """
    module = load_handler(monkeypatch, FakeAWS())
    token_metrics, other_metrics = set(), set()
    for entry in LIST_METRICS_FIXTURE["Metrics"]:
        name = entry["MetricName"]
        (token_metrics if name in module.CEILING else other_metrics).add(name)

    assert token_metrics == set(module.CEILING), "recorded token metrics differ from CEILING"
    # SearchUnits is real Bedrock usage with no token direction: Cohere Rerank bills
    # it and publishes no token metric at all. It is covered by the Cost Explorer arm,
    # never by the metered arm, and that is a deliberate division rather than a gap.
    assert "SearchUnits" in other_metrics

    model_ids = {e["Dimensions"][0]["Value"] for e in LIST_METRICS_FIXTURE["Metrics"]}
    assert len(model_ids) >= len("a dozen live models"[:12]), "fixture lost ModelId coverage"


# ---------------------------------------------------------------------------
# Upper-bound invariant guard. Warn-only, and it must stay that way: a guard that
# can attach the deny turns a monitoring fault into a production outage.
# ---------------------------------------------------------------------------
SETTLED = TODAY - dt.timedelta(days=2)


def _guard_module(monkeypatch, fake_aws, metered_total: float):
    module = load_handler(monkeypatch, fake_aws, stub_ce=False)
    module._metered_spend = lambda day_start, now: module.MeteredSpend(
        total=metered_total, degraded=False, failures=(), saw_datapoints=True
    )
    return module


def test_guard_warns_when_the_estimate_falls_below_settled_billing(
    monkeypatch: pytest.MonkeyPatch,
):
    fake_aws = FakeAWS(ssm=FakeSSM({"ce_day": "old"}, guard_done=False), ce=FakeCE(spend=THRESHOLD))
    module = _guard_module(monkeypatch, fake_aws, metered_total=float(1))

    module._guard_upper_bound_invariant(SETTLED, {})

    assert fake_aws.sns.published, "a violated upper bound must raise a warning"
    subject = fake_aws.sns.published[0]["Subject"]
    assert "upper-bound" in subject.lower() or "VIOLATED" in subject


def test_guard_is_silent_when_the_estimate_is_above_settled_billing(
    monkeypatch: pytest.MonkeyPatch,
):
    fake_aws = FakeAWS(ssm=FakeSSM({"ce_day": "old"}, guard_done=False), ce=FakeCE(spend=THRESHOLD))
    module = _guard_module(monkeypatch, fake_aws, metered_total=THRESHOLD * 2)

    module._guard_upper_bound_invariant(SETTLED, {})

    assert fake_aws.sns.published == []


def test_guard_skips_days_below_the_noise_floor(monkeypatch: pytest.MonkeyPatch):
    """Near-idle days are rounding, not signal."""
    tiny = THRESHOLD * 0.01
    fake_aws = FakeAWS(ssm=FakeSSM({"ce_day": "old"}, guard_done=False), ce=FakeCE(spend=tiny))
    module = _guard_module(monkeypatch, fake_aws, metered_total=float(0))

    module._guard_upper_bound_invariant(SETTLED, {})

    assert fake_aws.sns.published == []


def test_guard_never_touches_iam(monkeypatch: pytest.MonkeyPatch):
    """A monitoring fault must not be able to deny production Bedrock."""
    fake_aws = FakeAWS(ssm=FakeSSM({"ce_day": "old"}, guard_done=False), ce=FakeCE(spend=THRESHOLD))
    module = _guard_module(monkeypatch, fake_aws, metered_total=float(1))
    before_roles = dict(fake_aws.iam.role_policies)
    before_groups = dict(fake_aws.iam.group_policies)

    module._guard_upper_bound_invariant(SETTLED, {})

    assert fake_aws.iam.role_policies == before_roles
    assert fake_aws.iam.group_policies == before_groups
    assert fake_aws.iam.attached == [] if hasattr(fake_aws.iam, "attached") else True


def test_guard_runs_at_most_once_per_settled_day(monkeypatch: pytest.MonkeyPatch):
    fake_aws = FakeAWS(ssm=FakeSSM({"ce_day": "old"}, guard_done=False), ce=FakeCE(spend=THRESHOLD))
    module = _guard_module(monkeypatch, fake_aws, metered_total=THRESHOLD * 2)
    state: dict = {}

    module._guard_upper_bound_invariant(SETTLED, state)
    calls_after_first = fake_aws.ce.calls
    module._guard_upper_bound_invariant(SETTLED, state)

    assert state["guard_day"] == SETTLED.isoformat()
    assert fake_aws.ce.calls == calls_after_first, "guard should be idempotent within a day"


# ---------------------------------------------------------------------------
# Per-model attribution. Diagnostics only: enforcement is ModelId-independent by
# design, and nothing here may feed the decision.
# ---------------------------------------------------------------------------
def _attribution_lines(caplog) -> list[str]:
    return [r.message for r in caplog.records if r.message.startswith("attribution ")]


def _cloudwatch_with_models() -> FakeCloudWatch:
    return FakeCloudWatch(
        pages=[
            {
                "Metrics": [
                    {
                        "MetricName": "InputTokenCount",
                        "Dimensions": [
                            {"Name": "ModelId", "Value": "us.anthropic.claude-opus-4-8"}
                        ],
                    }
                ]
            }
        ],
        values_by_id={"a0": [float(len("tokens"))]},
    )


def test_attribution_is_silent_on_a_normal_tick(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    fake_aws = FakeAWS(cloudwatch=_cloudwatch_with_models())
    module = load_handler(monkeypatch, fake_aws)
    with caplog.at_level("INFO"):
        run_handler(module, float(1))

    assert _attribution_lines(caplog) == []
    # The enforcement path must not enumerate models.
    assert fake_aws.cloudwatch.paginator_calls == 0


def test_attribution_is_logged_on_the_trip_path(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    fake_aws = FakeAWS(cloudwatch=_cloudwatch_with_models())
    module = load_handler(monkeypatch, fake_aws)
    with caplog.at_level("INFO"):
        run_handler(module, THRESHOLD)

    lines = _attribution_lines(caplog)
    assert lines, "a trip should say which models were running"
    assert "claude-opus-4-8" in lines[0]
    assert "tokens=" in lines[0]
    # No prices in the diagnostic: enforcement pricing and attribution stay separate.
    assert "usd" not in lines[0].lower()
    assert fake_aws.cloudwatch.paginator_calls > 0


def test_attribution_failure_cannot_change_the_enforcement_outcome(
    monkeypatch: pytest.MonkeyPatch,
):
    """A diagnostic that can break enforcement is worse than no diagnostic."""
    broken = FakeCloudWatch(pages=[], values_by_id={})
    broken.get_paginator = lambda name: (_ for _ in ()).throw(client_error("ListMetrics"))
    fake_aws = FakeAWS(cloudwatch=broken)
    module = load_handler(monkeypatch, fake_aws)

    result = run_handler(module, THRESHOLD)

    assert result["deny_attached"] is True


# ---------------------------------------------------------------------------
# Terraform and README: the consumers the enforcement change left describing a
# design that no longer exists.
# ---------------------------------------------------------------------------
TF_PATH = Path(__file__).resolve().parents[2] / "infra" / "terraform" / "bedrock_spend_cap.tf"
README_PATH = (
    Path(__file__).resolve().parents[2] / "infra" / "terraform" / "bedrock-spend-cap" / "README.md"
)


def test_list_metrics_is_granted_for_attribution():
    tf = TF_PATH.read_text(encoding="utf-8")
    statement = tf[tf.index('Sid    = "ReadSpendSignals"') :][:600]
    for action in ("ce:GetCostAndUsage", "cloudwatch:GetMetricData", "cloudwatch:ListMetrics"):
        assert action in statement, f"{action} missing from ReadSpendSignals"


def test_readme_no_longer_documents_the_deleted_pricing_layer():
    readme = README_PATH.read_text(encoding="utf-8")
    for gone in (
        "Prices are derived from this account's own bills",
        "bedrock_cap_seed_rates",
        "UNKNOWN_RATE_PER_1K",
        "a published seed rate",
    ):
        assert gone not in readme, f"README still documents {gone!r}"


def test_readme_states_the_upper_bound_and_its_cost():
    # Collapse whitespace first: these assert CONTENT, and a prose reflow should not
    # be able to fail them.
    readme = " ".join(README_PATH.read_text(encoding="utf-8").split())
    assert "upper bound on real spend, not an estimate of it" in readme
    assert "priced at a ceiling rate for its direction" in readme
    # The accepted over-estimate must be stated, not left for a reader to discover
    # when an embedding job trips the cap.
    assert "trip the cap well below the configured dollar figure" in readme
    # And the measured band stays out of a public repo.
    assert "local-only" in readme
