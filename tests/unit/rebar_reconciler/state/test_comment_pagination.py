"""Comment pagination: get_comments must fetch ALL pages (bug 1f3d / epic 58b0 P1).

``get_comments`` ran ``acli … comment list --key K --json`` with NO ``--paginate``,
so ACLI returned only the FIRST PAGE (default ``--limit 50``, ``--order +created`` —
the 50 OLDEST comments) despite both docstrings claiming "Get all comments". The
outbound comment dedup then re-posted every comment beyond page 1 on every pass,
inflating 13 Jira issues to Jira's 5000-comment HARD cap. Root cause proven LIVE on
REB-155: the response was ``{isLast:false, maxResults:50, startAt:0, total:5000}`` —
50 of 5000 returned.

These pin BOTH halves of the fix against the REAL ``--paginate`` output shape:
  1. the argv includes ``--paginate`` (both get_comments call sites); and
  2. the parser flattens ACLI's CONCATENATED per-page objects. Live-verified: a
     5000-comment issue yields ~101 back-to-back ``{"comments": [...], "isLast": …}``
     objects — a single ``json.loads`` raises "Extra data" on page 2 and silently
     drops the rest.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from rebar_reconciler import acli as acli_mod
from rebar_reconciler import acli_cli_ops, acli_subprocess


def _page(comments: list[dict], is_last: bool, start_at: int, total: int) -> str:
    return json.dumps(
        {
            "comments": comments,
            "isLast": is_last,
            "maxResults": 50,
            "startAt": start_at,
            "total": total,
        }
    )


def _c(i: int) -> dict:
    return {"id": str(i), "body": f"comment {i}"}


# Real ACLI `comment list --paginate --json`: ONE object PER PAGE, concatenated
# (verified live on REB-155). Three pages, 5 comments total.
_MULTIPAGE = "\n".join(
    [
        _page([_c(1), _c(2)], False, 0, 5),
        _page([_c(3), _c(4)], False, 2, 5),
        _page([_c(5)], True, 4, 5),
    ]
)


def _make_client() -> acli_mod.AcliClient:
    return acli_mod.AcliClient(
        "https://example.atlassian.net", "user@example.com", "token", jira_project="TEST"
    )


@pytest.fixture
def capture(monkeypatch):
    calls: list[list[str]] = []

    def _fake_run_acli(cmd, *, acli_cmd=None, retry_on_timeout=False, **kw):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=_MULTIPAGE, stderr="")

    monkeypatch.setattr(acli_subprocess, "_run_acli", _fake_run_acli)
    return calls


def test_get_comments_argv_includes_paginate(capture):
    """BOTH get_comments call sites must pass --paginate, else only page 1 returns."""
    acli_cli_ops.get_comments("REB-1")
    _make_client().get_comments("REB-1")
    assert capture, "no acli call recorded"
    for cmd in capture:
        assert "--paginate" in cmd, f"get_comments argv missing --paginate: {cmd}"


def test_get_comments_cli_ops_flattens_all_pages(capture):
    """acli_cli_ops.get_comments must return comments from ALL concatenated pages."""
    got = acli_cli_ops.get_comments("REB-1")
    assert sorted(c["id"] for c in got) == ["1", "2", "3", "4", "5"], (
        f"expected all 5 comments across 3 pages; got {[c.get('id') for c in got]}"
    )


def test_get_comments_client_flattens_all_pages(capture):
    """AcliClient.get_comments must also flatten all pages (the second call site)."""
    got = _make_client().get_comments("REB-1")
    assert sorted(c["id"] for c in got) == ["1", "2", "3", "4", "5"], (
        f"expected all 5 comments across 3 pages; got {[c.get('id') for c in got]}"
    )
