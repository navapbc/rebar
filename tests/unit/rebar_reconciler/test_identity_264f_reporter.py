"""AC-coverage for 264f's reporter integration criterion (epic gnu-whale-ichor): the
reporter REST sub-call sets the resolved accountId; an HTTP 4xx (Modify-Reporter not
granted) or an unresolvable reporter degrades via a soft-fail alert, pops `reporter`
so other fields still apply, and never fails the sync. Placed under
tests/unit/rebar_reconciler/ for the package conftest.
"""

from __future__ import annotations

import subprocess
import urllib.error
from pathlib import Path

import pytest

import rebar
import rebar_reconciler.dispatch_one as dispatch


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for a in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "d@e.com"),
        ("git", "config", "user.name", "d"),
        ("git", "commit", "-q", "--allow-empty", "-m", "i"),
    ):
        subprocess.run(a, cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    # a reporter that maps to a Jira accountId
    rebar.create_identity(
        "Ada",
        "ada@example.com",
        mappings=[{"provider": "jira", "external_id": "acct-reporter"}],
        repo_root=str(repo),
    )
    return repo


class _StubClient:
    def __init__(self, fail: Exception | None = None) -> None:
        self._fail = fail
        self.set_reporter_calls: list[tuple[str, str]] = []

    def set_reporter(self, jira_key: str, account_id: str) -> None:
        self.set_reporter_calls.append((jira_key, account_id))
        if self._fail is not None:
            raise self._fail


def test_reporter_set_by_account_id_on_success(store: Path) -> None:
    client = _StubClient()
    fields = {"reporter": "ada@example.com", "title": "keep me"}
    dispatch._update_one_apply_reporter(fields, "REB-1", client)
    assert client.set_reporter_calls == [("REB-1", "acct-reporter")]
    assert "reporter" not in fields  # popped before the scalar filter
    assert fields["title"] == "keep me"  # other fields untouched


def test_reporter_http_4xx_degrades_softly(store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[str] = []
    monkeypatch.setattr(
        dispatch,
        "_record_reporter_alert",
        lambda kind, jira_key, reason: captured.append(kind),
    )
    err = urllib.error.HTTPError("http://x", 403, "Forbidden", {}, None)  # type: ignore[arg-type]
    client = _StubClient(fail=err)
    fields = {"reporter": "ada@example.com", "title": "still applies"}

    dispatch._update_one_apply_reporter(fields, "REB-2", client)  # must NOT raise

    assert captured == ["outbound-reporter-not-permitted"]
    assert "reporter" not in fields
    assert fields["title"] == "still applies"


def test_reporter_unresolved_degrades_softly(store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[str] = []
    monkeypatch.setattr(
        dispatch,
        "_record_reporter_alert",
        lambda kind, jira_key, reason: captured.append(kind),
    )
    client = _StubClient()
    fields = {"reporter": "nobody@nowhere.test", "title": "keep"}

    dispatch._update_one_apply_reporter(fields, "REB-3", client)  # must NOT raise

    assert captured == ["outbound-reporter-unresolved"]
    assert client.set_reporter_calls == []  # never called for an unresolved reporter
    assert "reporter" not in fields
