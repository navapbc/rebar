"""Verified-fake contract (hermetic half) — the fake honours the shape contract.

Epic f89d, story C (`59ef-ff3e-5aea-47b1`). A fake is only as good as its fidelity
to the real client; if ``FakeAcliClient`` (story A) drifts from the real
``AcliClient``, we reintroduce bug 0ee6's failure mode (tests pass against fictional
shapes). Per "Software Engineering at Google", the fix is the **verified fake**: the
fake carries contract tests written against the client's public interface, and the
IDENTICAL contract (``tests/_jira_shape_contract.py``) runs against BOTH:

  * the ``FakeAcliClient`` — HERE, in the (hermetic, fast) integration suite, with
    no opt-in or live credentials;
  * the real ``AcliClient`` against live Jira — opt-in, in
    ``tests/external/test_verified_fake_contract_live.py`` (the ``external`` marker
    + ``REBAR_RUN_EXTERNAL`` + creds), so it never flakes the default suite.

If the real Jira API changes shape, the live run goes red → re-capture the fixtures
(docs/jira-fixtures.md). If the fake is hand-edited to diverge, the contract goes
red — demonstrated hermetically by ``test_contract_catches_a_divergent_fake``.

Out of scope (resolved epic open question): validating against Jira Cloud's
published OpenAPI. The dual-run real-vs-fake comparison IS the honesty mechanism; an
OpenAPI leg is YAGNI for a single team.
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


# ---------------------------------------------------------------------------
# The honesty mechanism, demonstrated hermetically: a fake whose return shape
# diverges from the contract FAILS the contract — so a real-API drift the fake
# didn't track surfaces as a fake-contract failure, not a silent green pass.
# ---------------------------------------------------------------------------


class _DivergentFakeClient:
    """A fake that regressed to bug-0ee6's flat ``comments`` shape."""

    def get_comment_map(self, project: str, jql: str | None = None) -> dict[str, Any]:
        # WRONG: a flat list under the key instead of the nested {"comments": [...]}.
        return {"REB-1": [{"id": "1", "body": "hi"}]}


def test_contract_catches_a_divergent_fake() -> None:
    bad = _DivergentFakeClient()
    with pytest.raises(AssertionError):
        assert_comment_map_shape(bad.get_comment_map("REB"))
