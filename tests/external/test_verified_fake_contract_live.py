"""Verified-fake contract (live half) — the REAL AcliClient honours the contract.

Epic f89d, story C (`59ef-ff3e-5aea-47b1`). Runs the SAME shape contract
(``tests/_jira_shape_contract.py``) that the hermetic fake satisfies
(``tests/integration/rebar_reconciler/test_verified_fake_contract.py``) against the
REAL ``AcliClient`` over live Jira. This is the half that keeps the fake honest: if
Jira's REST shape drifts from what the fixtures captured, THIS run goes red — the
signal to re-capture (docs/jira-fixtures.md). It is the verified-fake's second
implementation in the SWE-at-Google sense.

Opt-in / inert by default: the ``external`` marker + ``tests/external/conftest.py``
make it skip unless ``REBAR_RUN_EXTERNAL=1``, and ``_skip`` additionally requires
live Jira creds + the ``acli`` binary. It never runs (or bills) in the default suite.

Note: ``search_issues`` shells out to ``acli`` (which reads ``acli auth login``
config), while the three map methods use the passed REST creds — so the search test
additionally needs an *authenticated* ``acli``, not merely the binary on PATH.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest
from _jira_shape_contract import (
    assert_comment_map_shape,
    assert_issuelinks_map_shape,
    assert_parent_map_shape,
    assert_search_shape,
)

pytestmark = pytest.mark.external

REPO_ROOT = Path(__file__).resolve().parents[2]
ENGINE_DIR = REPO_ROOT / "src" / "rebar" / "_engine"

# Keep the live run fast: scope every call to a tiny, stable key set.
_LIVE_JQL = "key in (REB-431, REB-430)"
_PROJECT = os.environ.get("JIRA_PROJECT", "REB")


def _live_jira_ready() -> bool:
    creds = all(os.environ.get(k) for k in ("JIRA_URL", "JIRA_USER", "JIRA_API_TOKEN"))
    return bool(creds) and shutil.which("acli") is not None


_skip = pytest.mark.skipif(not _live_jira_ready(), reason="no live Jira creds / acli binary")


def _build_client():
    if str(ENGINE_DIR) not in sys.path:
        sys.path.insert(0, str(ENGINE_DIR))
    from rebar_reconciler import acli

    return acli.AcliClient(
        jira_url=os.environ["JIRA_URL"],
        user=os.environ["JIRA_USER"],
        api_token=os.environ["JIRA_API_TOKEN"],
    )


@_skip
def test_real_search_issues_shape() -> None:
    assert_search_shape(_build_client().search_issues(_LIVE_JQL))


@_skip
def test_real_comment_map_shape() -> None:
    result = _build_client().get_comment_map(_PROJECT, _LIVE_JQL)
    assert_comment_map_shape(result)
    # Non-vacuity: the scoped keys are known to carry comments, so an empty result
    # would mean the seam this harness guards (nested comment.comments) went
    # unexercised — fail rather than pass silently.
    assert any(f.get("comments") for f in result.values()), "live comment seam unexercised"


@_skip
def test_real_issuelinks_map_shape() -> None:
    result = _build_client().get_issuelinks_map(_PROJECT, _LIVE_JQL)
    assert_issuelinks_map_shape(result)
    # Non-vacuity: REB-431 carries issue-links; an all-empty result would leave the
    # issuelink seam unexercised.
    assert any(links for links in result.values()), "live issuelink seam unexercised"


@_skip
def test_real_parent_map_shape() -> None:
    assert_parent_map_shape(_build_client().get_parent_map(_PROJECT, _LIVE_JQL))
