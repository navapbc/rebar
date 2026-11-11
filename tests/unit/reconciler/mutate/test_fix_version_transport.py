"""Per-item Jira fixVersions writes on both transports (ADR 0124, volcanic-prim-parakeet).

Cloud writes through ``_direct_rest_put_raw`` (captured at ``urllib.request.urlopen``);
DC writes through pycontribs ``Issue.update(update=...)`` on a fake issue. Neither path
may send a whole-list ``set`` or a ``fields.fixVersions`` replacement, which would
clobber a concurrent Jira-side edit.
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
from typing import Any

import pytest

from rebar.reconciler.adapters.jira import acli as acli_mod

pytestmark = pytest.mark.unit


class _Resp:
    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *_a: Any) -> None:
        return None

    def read(self) -> bytes:
        return b""


def _cloud_client() -> acli_mod.AcliClient:
    return acli_mod.AcliClient(  # type: ignore[call-arg]
        jira_url="https://example.atlassian.net", user="u", api_token="t"
    )


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> list[urllib.request.Request]:
    requests: list[urllib.request.Request] = []

    def _fake_urlopen(req: urllib.request.Request, *_a: Any, **_k: Any) -> _Resp:
        requests.append(req)
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    return requests


@pytest.mark.parametrize(
    ("verb", "item"),
    [("add_fix_version", "add"), ("remove_fix_version", "remove")],
)
def test_cloud_sends_one_per_item_update(captured: list, verb: str, item: str) -> None:
    getattr(_cloud_client(), verb)("REB-1", "2.4")
    (req,) = captured
    assert req.get_method() == "PUT"
    assert req.full_url == "https://example.atlassian.net/rest/api/3/issue/REB-1"
    body = json.loads(req.data)
    assert body == {"update": {"fixVersions": [{item: {"name": "2.4"}}]}}
    assert "fields" not in body
    assert "set" not in json.dumps(body)


def test_cloud_non_2xx_raises_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_urlopen(req: urllib.request.Request, *_a: Any, **_k: Any) -> Any:
        raise urllib.error.HTTPError(
            req.full_url,
            400,
            "Bad Request",
            None,  # type: ignore[arg-type]
            io.BytesIO(b'{"errors":{"fixVersions":"Version name 9.9 is not valid"}}'),
        )

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    with pytest.raises(urllib.error.HTTPError) as caught:
        _cloud_client().add_fix_version("REB-1", "9.9")
    assert caught.value.code == 400


class _FakeIssue:
    def __init__(self, sink: list[dict]) -> None:
        self._sink = sink

    def update(self, **kwargs: Any) -> None:
        self._sink.append(kwargs)


class _FakeDcClient:
    def __init__(self) -> None:
        self.updates: list[dict] = []
        self.issue_keys: list[str] = []

    def issue(self, key: str) -> _FakeIssue:
        self.issue_keys.append(key)
        return _FakeIssue(self.updates)


@pytest.mark.parametrize(
    ("verb", "item"),
    [("add_fix_version", "add"), ("remove_fix_version", "remove")],
)
def test_dc_sends_one_per_item_update(verb: str, item: str) -> None:
    from rebar.reconciler.adapters.jira_datacenter.transport import JiraDataCenterTransport

    client = _FakeDcClient()
    transport = JiraDataCenterTransport(client=client, project="DC")
    getattr(transport, verb)("DC-1", "2.4")
    assert client.issue_keys == ["DC-1"]
    assert client.updates == [{"update": {"fixVersions": [{item: {"name": "2.4"}}]}}]


def test_both_transports_satisfy_the_port() -> None:
    from rebar.reconciler._backend import _REQUIRED_TRANSPORT_MEMBERS
    from rebar.reconciler.adapters.jira_datacenter.transport import JiraDataCenterTransport

    for member in ("add_fix_version", "remove_fix_version"):
        assert member in _REQUIRED_TRANSPORT_MEMBERS
        assert callable(getattr(acli_mod.AcliClient, member))
        assert callable(getattr(JiraDataCenterTransport, member))


# ── Dispatch: the real batch path (outbound_pass payload -> batch_dispatch -> update_one) ──


def _plans(mutations: list) -> Any:
    from rebar.reconciler import ticket_planner

    _, plans = ticket_planner.plan_pass(
        pass_id="test-pass",
        local_snapshot={},
        remote_snapshot={},
        binding_view={},
        mode="",
        selection={"kind": None, "ids": []},
        limits={"max_changes": None},
        mutations=mutations,
    )
    return plans


def _apply_release_update(tmp_path: Any, client: Any, releases: list[dict]) -> dict:
    import time
    from unittest.mock import patch

    from rebar.reconciler import applier, scratch_retention
    from rebar.reconciler.mutation import Mutation, MutationAction, MutationDirection
    from rebar.reconciler.mutation_payloads import OutboundUpdatePayload

    mutation = Mutation(
        MutationDirection.outbound,
        MutationAction.update,
        "DIG-1",
        OutboundUpdatePayload(releases=tuple(releases), local_id="loc-1"),
        {},
    )
    pass_id = f"rel-{time.time_ns()}"
    with patch.object(applier, "_load_acli", return_value=client):
        applier.apply([mutation], pass_id, repo_root=tmp_path, ticket_plans=_plans([mutation]))
    manifest = scratch_retention.snapshots_dir(tmp_path) / f"{pass_id}.manifest.json"
    outcomes = json.loads(manifest.read_text()).get("mutations", [])
    return next(o for o in outcomes if o.get("key") == "DIG-1")


def _mock_client() -> Any:
    from unittest.mock import MagicMock

    client = MagicMock()
    client.update_issue.return_value = {"key": "DIG-1", "ok": True}
    client.get_issue_links.return_value = []
    return client


def test_release_records_reach_the_transport_through_the_batch_path(tmp_path: Any) -> None:
    client = _mock_client()
    outcome = _apply_release_update(
        tmp_path,
        client,
        [{"action": "add", "release": "2.4"}, {"action": "remove", "release": "2.3"}],
    )
    client.add_fix_version.assert_called_once_with("DIG-1", "2.4")
    client.remove_fix_version.assert_called_once_with("DIG-1", "2.3")
    assert outcome.get("releases_applied") == 2
    assert "silent_noop" not in outcome


def test_failed_write_logs_jira_body_and_next_item_still_runs(tmp_path: Any, capsys: Any) -> None:
    client = _mock_client()
    client.add_fix_version.side_effect = urllib.error.HTTPError(
        "https://example.atlassian.net/rest/api/3/issue/DIG-1",
        400,
        "Bad Request",
        None,  # type: ignore[arg-type]
        io.BytesIO(b'{"errors":{"fixVersions":"Version name 9.9 is not valid"}}'),
    )
    outcome = _apply_release_update(
        tmp_path,
        client,
        [{"action": "add", "release": "9.9"}, {"action": "remove", "release": "2.3"}],
    )
    client.remove_fix_version.assert_called_once_with("DIG-1", "2.3")
    assert outcome.get("releases_applied") == 1
    assert "Version name 9.9 is not valid" in capsys.readouterr().err


def test_all_release_writes_failing_trips_the_silent_noop_canary(tmp_path: Any) -> None:
    client = _mock_client()
    client.add_fix_version.side_effect = RuntimeError("write boom")
    outcome = _apply_release_update(tmp_path, client, [{"action": "add", "release": "2.4"}])
    assert outcome.get("releases_applied") == 0
    assert outcome.get("silent_noop") == ["releases"]
    assert (outcome.get("error") or "").startswith("silent-noop:")
