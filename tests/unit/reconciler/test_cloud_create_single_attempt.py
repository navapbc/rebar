"""A create whose response is lost must not become a second Jira issue
(bug highbred-fabled-lobo, REB-7141/REB-7142).

Two guards together: the ACLI transport never re-runs a ``jira workitem create`` after a
non-zero exit (the issue may already exist), and the create carries the ``rebar-id:``
identity label so the next pass's label search finds the landed issue and binds it.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from rebar.reconciler import dispatch_one
from rebar.reconciler.adapters.jira import acli_cli_ops, acli_subprocess
from rebar.reconciler.create_route import _coordinated_create_core

# Records each invocation's argv as one JSON line, then fails or succeeds according to the
# behaviour argument: "fail" exits 1 after recording (the write landed, the call failed),
# "429-then-ok" rejects the first call with a rate-limit error and records nothing for it.
_FAKE_ACLI = r"""
import json, sys
log, mode = sys.argv[1], sys.argv[2]
argv = sys.argv[3:]
if "search" in argv:  # the post-create existence check reads the issue back
    print(json.dumps([{"key": "REB-1", "fields": {}}]))
    raise SystemExit(0)
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
print(json.dumps({"key": "REB-1", "id": "10001"}))
"""


def _fake(log: Path, mode: str) -> list[str]:
    return [sys.executable, "-c", _FAKE_ACLI, str(log), mode]


def _records(log: Path) -> list[dict]:
    return (
        [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        if log.exists()
        else []
    )


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(acli_subprocess, "_backoff_sleep", lambda _s: None)


def test_a_failed_create_is_not_replayed(tmp_path: Path, no_sleep: None) -> None:
    log = tmp_path / "calls"
    with pytest.raises(subprocess.CalledProcessError):
        acli_subprocess._run_acli(
            ["jira", "workitem", "create", "--summary", "s", "--json"],
            acli_cmd=_fake(log, "fail"),
        )
    assert len(_records(log)) == 1, "a create that may have landed was run again"


def test_a_rate_limited_create_is_still_retried(tmp_path: Path, no_sleep: None) -> None:
    log = tmp_path / "calls"
    acli_subprocess._run_acli(
        ["jira", "workitem", "create", "--summary", "s", "--json"],
        acli_cmd=_fake(log, "429-then-ok"),
    )
    records = _records(log)
    assert len(records) == 2 and "rejected" in records[0] and "argv" in records[1]


def test_other_failed_commands_keep_their_retries(tmp_path: Path, no_sleep: None) -> None:
    log = tmp_path / "calls"
    with pytest.raises(subprocess.CalledProcessError):
        acli_subprocess._run_acli(
            ["jira", "workitem", "edit", "--key", "REB-1", "--json"],
            acli_cmd=_fake(log, "fail"),
        )
    assert len(_records(log)) == acli_subprocess._MAX_ATTEMPTS


def test_the_plain_create_carries_its_labels(tmp_path: Path) -> None:
    log = tmp_path / "calls"
    acli_cli_ops._create_issue_no_json(
        "REB", "Bug", "s", acli_cmd=_fake(log, "ok"), labels=["rebar-id:b23c"]
    )
    argv = _records(log)[0]["argv"]
    assert argv[argv.index("--label") + 1] == "rebar-id:b23c"


def test_the_json_create_carries_its_labels(tmp_path: Path, monkeypatch) -> None:
    sent: list[dict] = []

    def _capture(payload, **_kw):
        sent.append(dict(payload))
        return subprocess.CompletedProcess([], 0, json.dumps({"key": "REB-1"}), "")

    monkeypatch.setattr(acli_cli_ops, "_create_from_json_payload", _capture)
    monkeypatch.setattr(acli_cli_ops, "_verify_created_issue", lambda out, **_kw: json.loads(out))
    acli_cli_ops._create_issue_from_json("REB", "Bug", "s", 2, labels=["rebar-id:b23c"])
    assert sent[0]["labels"] == ["rebar-id:b23c"]


class _LostResponseJira:
    """A Jira whose first create lands but whose response is lost (the call raises)."""

    def __init__(self) -> None:
        self.issues: list[dict] = []

    def create_issue(self, ticket_data: dict) -> dict:
        key = f"REB-{7141 + len(self.issues)}"
        self.issues.append({"key": key, "labels": list(ticket_data.get("labels") or [])})
        if len(self.issues) == 1:
            raise subprocess.CalledProcessError(1, ["acli"], "", "connection reset")
        return {"key": key, "id": str(len(self.issues))}

    def search_issues(self, jql: str) -> list[dict]:
        label = jql.split('"')[1]
        return [i for i in self.issues if label in i["labels"]]

    def add_label(self, key: str, label: str) -> None:
        for issue in self.issues:
            if issue["key"] == key and label not in issue["labels"]:
                issue["labels"].append(label)

    def set_entity_property(self, *_a) -> None:
        return None


def test_a_lost_create_response_yields_one_issue(tmp_path: Path) -> None:
    jira = _LostResponseJira()
    mutation = {
        "action": "create",
        "local_id": "b23c-8220-e09f-448a",
        "fields": {"summary": "Bedrock spend-cap estimate", "issuetype": {"name": "Bug"}},
    }

    with pytest.raises(subprocess.CalledProcessError):
        dispatch_one.create_one(
            mutation, jira, repo_root=tmp_path, create_core=_coordinated_create_core
        )
    result = dispatch_one.create_one(
        mutation, jira, repo_root=tmp_path, create_core=_coordinated_create_core
    )

    assert len(jira.issues) == 1, jira.issues
    assert result is not None and result["status"] == "dedup-create-skipped"
    assert result["key"] == "REB-7141"


def test_the_cloud_client_forwards_string_labels_to_the_create(tmp_path: Path) -> None:
    from rebar.reconciler.adapters.jira.acli import AcliClient

    log = tmp_path / "calls"
    client = AcliClient(
        jira_url="", user="", api_token="", jira_project="REB", acli_cmd=_fake(log, "ok")
    )
    client.create_issue(
        {
            "ticket_type": "bug",
            "title": "s",
            "labels": ["rebar-id:b23c", {"action": "add", "label": "user-tag"}],
        }
    )
    argv = _records(log)[0]["argv"]
    assert argv[argv.index("--label") + 1] == "rebar-id:b23c", argv


def _seams(ticket_data: dict):
    from rebar.reconciler.create_route import _CoordinatedSeams, _CreateCapture

    return _CoordinatedSeams(
        "b23c", ticket_data, client=None, binding_store=None, capture=_CreateCapture()
    )


def test_the_identity_label_joins_existing_string_labels() -> None:
    assert _seams({"labels": ["keep"]}).create_payload()["labels"] == ["keep", "rebar-id:b23c"]


def test_the_identity_label_is_not_added_twice() -> None:
    payload = _seams({"labels": ["rebar-id:b23c"]}).create_payload()
    assert payload["labels"] == ["rebar-id:b23c"]


def test_label_operations_are_not_sent_as_labels() -> None:
    payload = _seams({"labels": [{"action": "add", "label": "user-tag"}]}).create_payload()
    assert payload["labels"] == ["rebar-id:b23c"]
