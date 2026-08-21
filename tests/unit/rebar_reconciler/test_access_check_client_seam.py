"""The access-check client seam must resolve ``AcliClient`` at CALL time (bug 2c4b).

``access_check`` binds ``AcliClient`` into its own globals at import and froze that
binding into ``run_access_check``'s ``client_cls`` default — a default expression is
evaluated ONCE, at import. Tests patch the seam on the DEFINING module
(``monkeypatch.setattr(acli, "AcliClient", Fake)``), so the patch reached the probe only
when ``access_check`` happened not to be imported yet. Once any earlier test in the
process had imported it, the real client was used and the probe made a LIVE network call.

These tests pin the seam by forcing the hostile order explicitly: ``access_check`` is
imported BEFORE the patch, so they are deterministic in isolation and do not depend on
what else ran first. Same invariant the ``_retry_sleep`` indirection already establishes
for ``sleep_fn`` (ticket 5ea3-76e5-480a-4464).
"""

from __future__ import annotations

import pytest

import rebar
from rebar._lib_ops import _engine_module

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
    access_check = _engine_module("rebar_reconciler.access_check")
    acli = _engine_module("rebar_reconciler.adapters.jira.acli")
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
    access_check = _engine_module("rebar_reconciler.access_check")
    acli = _engine_module("rebar_reconciler.adapters.jira.acli")

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
    access_check = _engine_module("rebar_reconciler.access_check")
    acli = _engine_module("rebar_reconciler.adapters.jira.acli")
    monkeypatch.setattr(acli, "AcliClient", _FakeClient)
    _FakeClient.instantiated = 0

    result, _lines, returncode = access_check.run_access_check(
        env={"JIRA_URL": "", "JIRA_USER": "", "JIRA_API_TOKEN": "", "JIRA_PROJECT": ""}
    )

    assert returncode == 2
    assert result["verdict"] == "INVALID"
    assert _FakeClient.instantiated == 0
