"""A Jira write that creates something must not be re-sent after an ambiguous failure
(bug oppressed-topsecret-chickadee, follow-up to highbred-fabled-lobo).

A connection fault or a non-zero ACLI exit can arrive AFTER Jira applied the write, so
replaying it can create a second issue or post a second comment. The ACLI transport already
refused to re-run ``jira workitem create``; this extends the rule to ``jira workitem comment
create`` and to the Data Center create and comment, where both rebar's retry loop and the
``jira`` library's own ``ResilientSession`` (``max_retries=3`` by default) used to re-send.

An ambiguous Data Center create is raised as a builtin ``ConnectionError`` so the create
coordinator re-observes it by its ``rebar-id:`` label instead of failing it as permanent.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from rebar.reconciler.adapters.jira import acli_cli_ops, acli_subprocess
from rebar.reconciler.adapters.jira_datacenter import retry as _retry
from rebar.reconciler.adapters.jira_datacenter import settings as dc_settings
from rebar.reconciler.adapters.jira_datacenter import transport as _t


class _ReqConnectionError(OSError):
    """Stands in for ``requests.exceptions.ConnectionError`` (an ``OSError``, NOT a builtin
    ``ConnectionError``), so the test runs without the ``[jira-datacenter]`` extra."""


class _ReqReadTimeout(OSError):
    """Stands in for ``requests.exceptions.ReadTimeout`` (also not a builtin ``TimeoutError``)."""


_AMBIGUOUS_FAULTS = [_ReqConnectionError, _ReqReadTimeout, TimeoutError]


@pytest.fixture
def dc_faults(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Make the stand-ins the transport's retryable set and capture sleeps instead of serving
    them (the unit tier has no ``requests``-bearing extra, so the real set may be smaller)."""
    monkeypatch.setattr(
        _retry,
        "_connection_retry_exceptions",
        lambda: (_ReqConnectionError, _ReqReadTimeout, TimeoutError),
    )
    slept: list[float] = []
    monkeypatch.setattr(_retry.time, "sleep", lambda s: slept.append(s))
    return slept


# ---------------------------------------------------------------------------
# Cloud: ``jira workitem comment create``
# ---------------------------------------------------------------------------

_FAKE_ACLI = r"""
import json, sys
log, mode = sys.argv[1], sys.argv[2]
argv = sys.argv[3:]
try:
    seen = sum(1 for _ in open(log))
except FileNotFoundError:
    seen = 0
if mode == "429-then-ok" and seen == 0:
    open(log, "a").write(json.dumps({"rejected": argv}) + "\n")
    sys.stderr.write("Error: 429 Too Many Requests")
    raise SystemExit(1)
open(log, "a").write(json.dumps({"argv": argv}) + "\n")
if mode == "fail":
    sys.stderr.write("Error: upstream connection reset")
    raise SystemExit(1)
print(json.dumps({"id": "10001", "body": "b"}))
"""


def _fake_acli(log: Path, mode: str) -> list[str]:
    return [sys.executable, "-c", _FAKE_ACLI, str(log), mode]


def _records(log: Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


@pytest.fixture
def no_acli_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(acli_subprocess, "_backoff_sleep", lambda _s: None)


def test_a_failed_cloud_comment_create_is_not_replayed(tmp_path: Path, no_acli_sleep: None) -> None:
    log = tmp_path / "calls"
    with pytest.raises(subprocess.CalledProcessError):
        acli_cli_ops.add_comment("REB-1", "hello", acli_cmd=_fake_acli(log, "fail"))
    records = _records(log)
    assert len(records) == 1, f"a comment that may have landed was posted again: {records}"
    assert records[0]["argv"][:4] == ["jira", "workitem", "comment", "create"]


def test_a_rate_limited_cloud_comment_create_is_still_retried(
    tmp_path: Path, no_acli_sleep: None
) -> None:
    log = tmp_path / "calls"
    acli_cli_ops.add_comment("REB-1", "hello", acli_cmd=_fake_acli(log, "429-then-ok"))
    records = _records(log)
    assert len(records) == 2 and "rejected" in records[0] and "argv" in records[1], records


# ---------------------------------------------------------------------------
# Data Center: create and comment through the real transport
# ---------------------------------------------------------------------------


class _LibObj:
    def __init__(self, raw: dict[str, Any]) -> None:
        self.raw = raw


class _LandsThenFaults:
    """A ``jira.JIRA`` stand-in whose writes land server-side, then lose their response."""

    def __init__(self, fault: type[BaseException]) -> None:
        self.fault = fault
        self.creates: list[dict[str, Any]] = []
        self.comments: list[tuple[str, str]] = []
        self.reads = 0

    def create_issue(self, **fields: Any) -> Any:
        self.creates.append(fields)
        raise self.fault("response lost after the create landed")

    def add_comment(self, key: str, body: str) -> Any:
        self.comments.append((key, body))
        raise self.fault("response lost after the comment landed")

    def issue(self, key: str, **_k: Any) -> Any:
        self.reads += 1
        raise self.fault("read failed")


@pytest.mark.parametrize("fault", _AMBIGUOUS_FAULTS, ids=lambda f: f.__name__)
def test_a_dc_create_that_may_have_landed_is_sent_once(
    fault: type[BaseException], dc_faults: list[float]
) -> None:
    client = _LandsThenFaults(fault)
    transport = _t.JiraDataCenterTransport(client=client, project="DC")

    with pytest.raises((ConnectionError, TimeoutError)) as caught:
        transport.create_issue({"summary": "s", "issuetype": {"name": "Task"}})

    assert len(client.creates) == 1, f"the create was sent {len(client.creates)} times"
    assert dc_faults == [], "slept before a retry that must not happen"
    # The coordinator treats ONLY builtin ConnectionError/TimeoutError as ambiguous.
    assert type(caught.value) in (ConnectionError, TimeoutError), type(caught.value)


def test_a_dc_create_does_not_prefetch_inside_the_unreplayable_call(dc_faults: list[float]) -> None:
    """``prefetch=True`` adds a GET after the POST inside the same call; the non-replayable
    unit must be the POST alone."""
    sent: list[dict[str, Any]] = []

    class _Ok:
        def create_issue(self, **fields: Any) -> Any:
            sent.append(fields)
            return _LibObj({"id": "10001", "key": "DC-9", "self": "x"})

    transport = _t.JiraDataCenterTransport(client=_Ok(), project="DC")
    result = transport.create_issue({"summary": "s", "issuetype": {"name": "Task"}})

    assert sent[0].get("prefetch") is False, sent[0]
    assert result["key"] == "DC-9"


@pytest.mark.parametrize("fault", _AMBIGUOUS_FAULTS, ids=lambda f: f.__name__)
def test_a_dc_comment_that_may_have_landed_is_sent_once(
    fault: type[BaseException], dc_faults: list[float]
) -> None:
    client = _LandsThenFaults(fault)
    transport = _t.JiraDataCenterTransport(client=client, project="DC")

    with pytest.raises((ConnectionError, TimeoutError)):
        transport.add_comment("DC-1", "body")

    assert len(client.comments) == 1, f"the comment was sent {len(client.comments)} times"


def test_dc_reads_keep_their_connection_retry(dc_faults: list[float]) -> None:
    client = _LandsThenFaults(_ReqConnectionError)
    transport = _t.JiraDataCenterTransport(client=client, project="DC")

    with pytest.raises(_ReqConnectionError):
        transport.get_issue("DC-1")

    assert client.reads == 3, f"a read made {client.reads} attempts; 3 is the policy"


class _LabelSearchable(_LandsThenFaults):
    """Records the landed issue, so the coordinator's label search can find it."""

    def create_issue(self, **fields: Any) -> Any:
        self.landed = {"key": "DC-7141", "id": "7141", "fields": {"labels": fields["labels"]}}
        return super().create_issue(**fields)

    def search_issues(self, jql: str, **_k: Any) -> list[Any]:
        label = jql.split('"')[1]
        landed = getattr(self, "landed", None)
        if landed and label in landed["fields"]["labels"] and _k.get("startAt", 0) == 0:
            return [_LibObj(landed)]
        return []


def test_a_lost_dc_create_is_recovered_by_label_not_replayed(dc_faults: list[float]) -> None:
    """End to end over the coordinator: one create, and the landed issue's key is found by
    its identity label in the same pass instead of the create failing as permanent."""
    from rebar.reconciler.create_route import _run_coordinated_create

    client = _LabelSearchable(_ReqConnectionError)
    transport = _t.JiraDataCenterTransport(client=client, project="DC")

    outcome, capture = _run_coordinated_create(
        "b23c-8220-e09f-448a",
        {"summary": "s", "issuetype": {"name": "Task"}},
        client=transport,
        binding_store=None,
    )

    assert len(client.creates) == 1, client.creates
    assert capture.create_error is None, f"treated as permanent: {capture.create_error!r}"
    assert outcome.has_known_key() and outcome.known_key == "DC-7141", outcome


# ---------------------------------------------------------------------------
# Data Center: the library must not re-send; reads it served keep a rebar retry
# ---------------------------------------------------------------------------


def _dc_settings() -> dc_settings.JiraDataCenterSettings:
    return dc_settings.JiraDataCenterSettings(
        url="https://jira.example.invalid",
        project="DC",
        allow_insecure=False,
        ca_bundle="",
        pat="pat-xyz",
    )


def test_the_dc_client_is_built_without_library_retries(
    monkeypatch: pytest.MonkeyPatch, dc_faults: list[float]
) -> None:
    built: list[dict[str, Any]] = []

    def _ctor(*_a: Any, **kwargs: Any) -> object:
        built.append(kwargs)
        if len(built) == 1:  # the constructor's serverInfo GET faults once
            raise _ReqConnectionError("connect reset")
        return object()

    monkeypatch.setattr(_t, "_jira_client_class", lambda: _ctor)
    _t.build_client_from_settings(_dc_settings())

    assert len(built) == 2, "client construction (a read) was not retried"
    assert all(k.get("max_retries") == 0 for k in built), built


def test_the_mapping_probe_retries_its_reads(
    monkeypatch: pytest.MonkeyPatch, dc_faults: list[float]
) -> None:
    from rebar.reconciler import mapping_probe

    calls: list[int] = []

    class _FlakyClient:
        def statuses(self) -> list[Any]:
            calls.append(1)
            if len(calls) == 1:
                raise _ReqConnectionError("reset")
            return [_LibObj({"name": "Open"})]

    monkeypatch.setattr(dc_settings, "resolve_jira_datacenter_settings", _dc_settings)
    monkeypatch.setattr(_t, "build_client_from_settings", lambda _s: _FlakyClient())
    port = mapping_probe.build_probe()

    port.statuses()
    assert len(calls) == 2, "a probe read was not retried after a connection fault"


def test_the_dc_user_search_retries(dc_faults: list[float]) -> None:
    from rebar.reconciler.adapters.jira_datacenter.backend import _search_users_by_username

    calls: list[int] = []

    class _FlakyClient:
        def search_users(self, **_k: Any) -> list[Any]:
            calls.append(1)
            if len(calls) == 1:
                raise _ReqConnectionError("reset")
            return []

    _search_users_by_username(_FlakyClient(), "jdoe")
    assert len(calls) == 2


def test_the_epic_link_field_lookup_retries(dc_faults: list[float]) -> None:
    calls: list[int] = []

    class _FlakyClient:
        def fields(self) -> list[dict[str, Any]]:
            calls.append(1)
            if len(calls) == 1:
                raise _ReqConnectionError("reset")
            return [{"id": "customfield_10008", "name": "Epic Link"}]

    transport = _t.JiraDataCenterTransport(client=_FlakyClient(), project="DC")
    assert transport._resolve_epic_link_field_id() == "customfield_10008"
    assert len(calls) == 2
