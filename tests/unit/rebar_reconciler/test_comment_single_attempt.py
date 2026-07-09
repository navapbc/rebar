"""Story 9622 (D2): outbound comments post SINGLE-attempt (no _call_with_retry).

A comment has no cheap Jira-side idempotency key, so retrying a possibly-landed
post could DUPLICATE it. Comments are therefore unwrapped from _call_with_retry at
BOTH dispatch_one.py:341 (create path) and :610 (update path): one attempt, and a
failure is recorded to the non-fatal comment_errors list (the comment differ
re-emits it next pass — eventually-consistent).

Proof that the unwrap is real: a RETRYABLE error (TimeoutError) is injected. Under
the OLD wrapped code _call_with_retry would retry it (add_comment called 4x); with
the single-attempt code it is called EXACTLY once. RetryExhaustedError (a named
subprocess-floor failure) is also exercised for the recorded-and-continue path.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
DISPATCH_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "dispatch_one.py"


def _load_dispatch():
    spec = importlib.util.spec_from_file_location("dispatch_one_comment_test", DISPATCH_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["dispatch_one_comment_test"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def dispatch():
    if not DISPATCH_PATH.exists():
        pytest.fail(f"dispatch_one.py not found at {DISPATCH_PATH}")
    return _load_dispatch()


def _make_exc(dispatch, kind: str) -> Exception:
    if kind == "retryable":
        # TimeoutError IS retryable by _call_with_retry — under the OLD wrapped
        # code this would drive 4 add_comment calls, so call_count==1 proves the unwrap.
        return TimeoutError("transient")
    # RetryExhaustedError — a real subprocess-floor exhaustion type named by the AC.
    return dispatch.RetryExhaustedError("acli failed", last_exception=None, attempts=4)


@pytest.mark.parametrize("kind", ["retryable", "named"])
def test_create_path_comment_single_attempt(dispatch, kind, tmp_path):
    """create_one (:341): add_comment is called exactly once and the failure is
    recorded to comment_errors — never retried."""
    client = MagicMock()
    client.search_issues.return_value = []  # no dedup hit -> real create
    client.create_issue.return_value = {"key": "DIG-1"}
    client.add_comment.side_effect = _make_exc(dispatch, kind)
    mutation = {
        "local_id": "cmt-create",
        "action": "create",
        "fields": {"summary": "s", "issuetype": {"name": "Task"}},
        "comments": [{"body": "hello"}],
    }
    comment_errors: list[str] = []
    dispatch.create_one(mutation, client, repo_root=tmp_path, comment_errors=comment_errors)
    assert client.add_comment.call_count == 1, "comment must be a SINGLE attempt (no retry)"
    assert len(comment_errors) == 1
    assert "add_comment failed" in comment_errors[0]


@pytest.mark.parametrize("kind", ["retryable", "named"])
def test_update_path_comment_single_attempt(dispatch, kind):
    """update_one (:610): add_comment is called exactly once and recorded — no retry."""
    client = MagicMock()
    client.update_issue.return_value = {"key": "DIG-2", "ok": True}
    client.add_comment.side_effect = _make_exc(dispatch, kind)
    mutation = {
        "action": "update",
        "key": "DIG-2",
        "fields": {"summary": "s2"},
        "comments": [{"body": "world"}],
    }
    comment_errors: list[str] = []
    dispatch.update_one(mutation, client, comment_errors=comment_errors)
    assert client.add_comment.call_count == 1, "comment must be a SINGLE attempt (no retry)"
    assert len(comment_errors) == 1
    assert "add_comment failed" in comment_errors[0]
