"""Access checks must resolve AcliClient when called (bug 2c4b).

Import access_check before patching AcliClient on its defining module. This
pins the late-bound seam and prevents import order from selecting the client
or making a network call. The retry-sleep seam follows the same rule."""

from __future__ import annotations

import importlib

import pytest

import rebar

pytestmark = pytest.mark.unit

_STEPS = [
    "STEP_CREATE",
    "STEP_LABEL",
    "STEP_PROPERTY_WRITE",
    "STEP_JQL_SEARCH",
    "STEP_PROPERTY_READ",
    "STEP_DELETE",
]


class _FakeClient:
    """A complete stand-in for AcliClient across all six probe steps."""

    instantiated = 0

    def __init__(self, **_kwargs) -> None:
        type(self).instantiated += 1
        self.issue_key = "DIG-1"
        self.property_value: str | None = None

    def create_issue(self, _fields):
        return {"key": self.issue_key}

    def _direct_rest_put_raw(self, _path, _body):
        return None

    def set_issue_property(self, _key, _name, value):
        self.property_value = value

    def search_issues(self, _jql):
        return [{"key": self.issue_key}]

    def get_issue_property(self, _key, _name):
        return self.property_value

    def delete_issue(self, _key):
        return None


@pytest.fixture
def probe_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JIRA_URL", "https://example.atlassian.net")
    monkeypatch.setenv("JIRA_USER", "operator@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "secret")
    monkeypatch.setenv("JIRA_PROJECT", "DIG")


def test_client_patch_reaches_the_probe_after_access_check_is_already_imported(
    monkeypatch: pytest.MonkeyPatch,
    probe_env: None,
) -> None:
    """The hostile order: import the consumer FIRST, then patch the defining module."""
    # PRECONDITION: access_check is resident in sys.modules before the patch is applied,
    # which is exactly the state a full-suite run leaves it in.
    access_check = importlib.import_module("rebar.reconciler.access_check")
    acli = importlib.import_module("rebar.reconciler.adapters.jira.acli")
    assert access_check.run_access_check is not None

    monkeypatch.setattr(acli, "AcliClient", _FakeClient)
    _FakeClient.instantiated = 0

    result = rebar.bridge_check_access()

    assert _FakeClient.instantiated == 1, "the probe did not use the patched client class"
    assert result["verdict"] == "PASS"
    assert [step["step"] for step in result["steps"]] == _STEPS
    assert all(step["passed"] is True for step in result["steps"])


def test_an_explicitly_passed_client_cls_still_wins_over_the_seam(
    monkeypatch: pytest.MonkeyPatch,
    probe_env: None,
) -> None:
    """Call-time resolution must not override an explicit injection (collateral invariant)."""
    access_check = importlib.import_module("rebar.reconciler.access_check")
    acli = importlib.import_module("rebar.reconciler.adapters.jira.acli")

    class _Unused(_FakeClient):
        pass

    monkeypatch.setattr(acli, "AcliClient", _Unused)
    _FakeClient.instantiated = 0
    _Unused.instantiated = 0

    result, _lines, returncode = access_check.run_access_check(client_cls=_FakeClient)

    assert _FakeClient.instantiated == 1
    assert _Unused.instantiated == 0
    assert returncode == 0
    assert result["verdict"] == "PASS"


def test_the_probe_still_refuses_to_run_without_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail-closed credential handling is unchanged by the seam (collateral invariant)."""
    access_check = importlib.import_module("rebar.reconciler.access_check")
    acli = importlib.import_module("rebar.reconciler.adapters.jira.acli")
    monkeypatch.setattr(acli, "AcliClient", _FakeClient)
    _FakeClient.instantiated = 0

    result, _lines, returncode = access_check.run_access_check(
        env={"JIRA_URL": "", "JIRA_USER": "", "JIRA_API_TOKEN": "", "JIRA_PROJECT": ""}
    )

    assert returncode == 2
    assert result["verdict"] == "INVALID"
    assert _FakeClient.instantiated == 0
