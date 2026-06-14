"""Tests for applier.update_one comment-fallback on 400 illegal-transition.

When Jira rejects a status transition because it is not legal from the current
workflow state (HTTP 400 with body containing 'illegal' or 'transition'),
update_one must:

  1. Not retry update_issue (zero retries on 400 — it is a state error).
  2. Call client.add_comment(issue_key, comment) where comment contains the
     substring 'local status changed to <status>'.
  3. Emit a structured JSON log record on stderr with keys
     {action, issue_key, attempted_status, reason}.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
APPLIER_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "applier.py"
)


def _load_applier():
    spec = importlib.util.spec_from_file_location("applier", APPLIER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["applier"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def applier():
    return _load_applier()


def _make_illegal_transition_exc(applier_mod):
    """Build a JiraAPIError that looks like a 400 illegal-transition."""
    return applier_mod.JiraAPIError(
        "Illegal status transition from 'Done' to 'In Progress'",
        status_code=400,
    )


def test_400_illegal_transition_falls_back_to_comment(applier):
    """update_one calls client.add_comment once with the local-status sentinel."""
    client = MagicMock()
    client.update_issue.side_effect = _make_illegal_transition_exc(applier)

    mutation = {
        "action": "update",
        "key": "DIG-123",
        "fields": {"status": "In Progress"},
    }

    result = applier.update_one(mutation, client)

    assert result is None, "comment-fallback path must return None, not re-raise"
    client.add_comment.assert_called_once()
    call_args = client.add_comment.call_args
    # first positional is issue_key, second positional is body
    assert call_args.args[0] == "DIG-123"
    body = call_args.args[1]
    assert "local status changed to " in body, (
        f"add_comment body must contain the sentinel; got {body!r}"
    )
    assert "In Progress" in body


def test_zero_update_issue_retries_on_400(applier):
    """update_issue must be called exactly once on a 400 illegal-transition (no retry)."""
    client = MagicMock()
    client.update_issue.side_effect = _make_illegal_transition_exc(applier)

    mutation = {
        "action": "update",
        "key": "DIG-456",
        "fields": {"status": "Closed"},
    }

    applier.update_one(mutation, client)

    assert client.update_issue.call_count == 1, (
        f"400 illegal-transition must not be retried; "
        f"got {client.update_issue.call_count} calls"
    )


def test_structured_log_emitted(applier, capsys):
    """A JSON log record is written to stderr describing the fallback."""
    client = MagicMock()
    client.update_issue.side_effect = _make_illegal_transition_exc(applier)

    mutation = {
        "action": "update",
        "key": "DIG-789",
        "fields": {"status": "Done"},
    }

    applier.update_one(mutation, client)

    captured = capsys.readouterr()
    # Find the JSON line in stderr
    stderr_lines = [line for line in captured.err.splitlines() if line.strip()]
    parsed = None
    for line in stderr_lines:
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("action") == "comment_fallback":
            parsed = obj
            break

    assert parsed is not None, (
        f"expected a JSON log record on stderr with action=comment_fallback; "
        f"stderr was: {captured.err!r}"
    )
    assert parsed["issue_key"] == "DIG-789"
    assert parsed["attempted_status"] == "Done"
    assert parsed["reason"] == "400_illegal_transition"


def test_non_illegal_400_still_raises(applier):
    """A 400 without 'illegal'/'transition' wording must propagate, not fall back."""
    client = MagicMock()
    client.update_issue.side_effect = applier.JiraAPIError(
        "Bad request: missing required field 'summary'",
        status_code=400,
    )

    mutation = {
        "action": "update",
        "key": "DIG-999",
        "fields": {"summary": ""},
    }

    with pytest.raises(applier.JiraAPIError):
        applier.update_one(mutation, client)

    client.add_comment.assert_not_called()
