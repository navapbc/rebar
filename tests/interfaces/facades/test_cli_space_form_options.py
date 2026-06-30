"""Read-CLI value-options accept BOTH ``--opt value`` (space) and ``--opt=value``.

Regression for bug ``flap-meal-boast`` (ae39-7afc-c500-497d): the read-side CLI
arms only matched the equals form, so ``session-logs --limit 30`` (space) failed
with ``unknown option`` / exit 2 while ``--limit=30`` worked — inconsistent with
``ready --epic <id>``, the write/composer commands (``claim --assignee <you>``),
``--output json``, and the docs, which all accept the space form. An agent
following the documented ``--opt <value>`` convention saw an empty piped result
and wrongly concluded there were no rows. These tests pin accept-both parity.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import rebar
from rebar._cli import main


def _json_out(capsys: pytest.CaptureFixture[str]) -> object:
    return json.loads(capsys.readouterr().out)


def test_list_status_space_form_matches_equals(
    rebar_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``list --status open`` (space) returns the same rows as ``--status=open``."""
    tid = rebar.create_ticket("task", "space-form list smoke", repo_root=str(rebar_repo))

    assert main(["list", "--status=open"]) == 0
    equals_rows = _json_out(capsys)

    assert main(["list", "--status", "open"]) == 0, "space form must not be a usage error"
    space_rows = _json_out(capsys)

    assert space_rows == equals_rows
    assert any(t["ticket_id"] == tid for t in space_rows)


def test_session_logs_limit_space_form_matches_equals(
    rebar_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``session-logs --limit 30`` (space) returns the same as ``--limit=30``."""
    rebar.append_session_log("space-form session-log smoke", repo_root=str(rebar_repo))

    assert main(["session-logs", "--limit=30"]) == 0
    equals_rows = _json_out(capsys)

    assert main(["session-logs", "--limit", "30"]) == 0, "space form must not be a usage error"
    space_rows = _json_out(capsys)

    assert space_rows == equals_rows
    assert len(space_rows) >= 1


def test_search_status_space_form_accepted(
    rebar_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``search <q> --status open`` (space) parity with the equals form."""
    rebar.create_ticket("task", "needle-in-haystack", repo_root=str(rebar_repo))

    assert main(["search", "needle", "--status=open"]) == 0
    equals_rows = _json_out(capsys)

    assert main(["search", "needle", "--status", "open"]) == 0
    space_rows = _json_out(capsys)

    assert space_rows == equals_rows
