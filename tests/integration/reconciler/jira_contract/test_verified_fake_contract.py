"""Run the shared Jira shape contract against the hermetic fake. The same contract runs
against the real client in the opt-in external tier; a deliberately divergent fake
proves this check rejects fictional shapes.
"""

from __future__ import annotations

from typing import Any

import pytest
from _fakes import FakeAcliClient
from _jira_shape_contract import (
    assert_comment_map_shape,
    assert_issuelinks_map_shape,
    assert_parent_map_shape,
    assert_search_shape,
)

pytestmark = pytest.mark.integration


def test_fake_search_issues_shape() -> None:
    assert_search_shape(FakeAcliClient().search_issues("project = REB"))


def test_fake_comment_map_shape() -> None:
    assert_comment_map_shape(FakeAcliClient().get_comment_map("REB"))


def test_fake_issuelinks_map_shape() -> None:
    assert_issuelinks_map_shape(FakeAcliClient().get_issuelinks_map("REB"))


def test_fake_parent_map_shape() -> None:
    assert_parent_map_shape(FakeAcliClient().get_parent_map("REB"))


# A divergent fake must fail the shared contract instead of hiding shape drift.


class _DivergentFakeClient:
    """A fake that regressed to bug-0ee6's flat ``comments`` shape."""

    def get_comment_map(self, project: str, jql: str | None = None) -> dict[str, Any]:
        # WRONG: a flat list under the key instead of the nested {"comments": [...]}.
        return {"REB-1": [{"id": "1", "body": "hi"}]}


def test_contract_catches_a_divergent_fake() -> None:
    bad = _DivergentFakeClient()
    with pytest.raises(AssertionError):
        assert_comment_map_shape(bad.get_comment_map("REB"))
