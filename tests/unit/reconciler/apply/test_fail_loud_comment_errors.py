"""Observability (48c8-5375-f883-462d) — fail-loud on swallowed comment failures
and same-kind silent-no-op coverage for comments/labels (not just links).

Two gaps let a reconcile pass report ``OK: applied N of N`` / exit 0 while a sub-op
silently failed:

  * ``comment_errors`` (a swallowed PARTIAL add_comment failure — apply_handlers
    create + update paths) were recorded NON-fatally and never counted toward
    ``mutation_failures``. The pre-existing silent-no-op canary only catches a
    TOTAL no-op (computed > 0, applied == 0); a PARTIAL drop (e.g. 2 comments
    computed, 1 applied, 1 dropped) leaves ``applied > 0`` so the canary never
    fires — yet a comment was silently lost and the pass still exits 0. This is
    the exact gap the ticket names ("a partial comment drop yields exit 0").
  * the silent-no-op canary's *test* coverage was links-only, even though the code
    iterates ``("labels", "comments", "links")``.

This pins:

  * **Fail-loud, PARTIAL drop** — with no environment set, a
    ``comment_errors``-carrying outcome whose canary did NOT fire (applied > 0) still
    gets an ``outcome["error"]`` starting ``comment-errors:`` (so reconcile.py counts
    it toward ``mutation_failures`` → non-zero exit). Both paths (create + update) are
    asserted — RED before the apply_handlers change (comment_errors → error=None on a
    partial drop).
  * **Same-kind canary coverage** — a TOTAL no-op (computed > 0, applied == 0) on
    COMMENTS and on LABELS trips ``silent_noop`` and hard-fails the mutation, matching
    the links coverage already in test_silent_noop_canary.py.

Asserts the outcome flags/error, not specific log strings.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

from rebar.reconciler import scratch_retention

_ACLI_FAIL = "ACLI mutation reported FAILURE (exit=0): comment body too long"

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPTS_DIR = REPO_ROOT / "src" / "rebar" / "_engine"
APPLIER_PATH = SCRIPTS_DIR.parent / "reconciler" / "applier.py"
ACLI_PATH = SCRIPTS_DIR.parent / "reconciler" / "adapters" / "jira" / "acli.py"


@pytest.fixture(scope="module")
def applier_mod() -> Iterator[ModuleType]:
    name = "applier_fail_loud_comment_errors"
    mod = importlib.import_module("rebar.reconciler.applier")
    try:
        yield mod
    finally:
        sys.modules.pop(name, None)


@pytest.fixture(scope="module")
def acli_mod() -> Iterator[ModuleType]:
    name = "acli_fail_loud_comment_errors"
    mod = importlib.import_module("rebar.reconciler.adapters.jira.acli")
    try:
        yield mod
    finally:
        sys.modules.pop(name, None)


def _make_fake_acli(acli_mod: ModuleType, client: MagicMock) -> MagicMock:
    # S4: _load_acli returns the transport directly.
    return client


def _apply_one(applier_mod, acli_mod, mutation, client, tmp_path, pass_id) -> dict:
    fake = _make_fake_acli(acli_mod, client)
    with patch.object(applier_mod, "_load_acli", return_value=fake):
        applier_mod.apply([mutation], pass_id, repo_root=tmp_path)
    manifest = scratch_retention.snapshots_dir(tmp_path) / f"{pass_id}.manifest.json"
    outcomes = json.loads(manifest.read_text()).get("mutations", []) if manifest.is_file() else []
    return next((o for o in outcomes if o.get("key") == mutation["key"]), {})


def _update_with_partial_comment_drop(acli_mod, *, key: str) -> tuple[dict, MagicMock]:
    """A mutation with TWO comments where the FIRST succeeds and the SECOND fails.
    -> comments_computed=2, comments_applied=1 (canary does NOT fire), one
    comment_errors entry. This is the partial-drop gap the canary can't see."""
    mutation = {
        "direction": "outbound",
        "action": "update",
        "key": key,
        "fields": {"summary": "scalar update that succeeds"},
        "comments": [
            {"action": "add", "body": "first comment that lands"},
            {"action": "add", "body": "second comment whose add fails"},
        ],
        "local_id": f"loc-{key}",
    }
    client = MagicMock()
    client.update_issue.return_value = {"key": key, "ok": True}
    client.add_comment.side_effect = [
        {"id": "1", "body": "first comment that lands"},
        acli_mod.AcliMutationError(_ACLI_FAIL),
    ]
    return mutation, client


# --------------------------------------------------------------------------- #
# Fail-loud: a PARTIAL comment drop -> mutation failure, with no environment set.
# (The pre-existing canary catches only TOTAL no-ops, so this is genuinely new.)
# --------------------------------------------------------------------------- #
def test_partial_comment_drop_promotes_to_error(applier_mod, acli_mod, tmp_path) -> None:
    mut, client = _update_with_partial_comment_drop(acli_mod, key="DIG-9100")
    outcome = _apply_one(applier_mod, acli_mod, mut, client, tmp_path, f"fl-on-{time.time_ns()}")
    # Partial: one comment landed, so the canary does NOT fire — proving this
    # error comes from the comment_errors promotion, not the silent-no-op canary.
    assert outcome.get("comments_applied") == 1
    assert "comments" not in (outcome.get("silent_noop") or []), (
        "a partial drop (applied>0) must NOT trip the total-no-op canary; "
        f"got silent_noop={outcome.get('silent_noop')}"
    )
    assert outcome.get("comment_errors"), "the dropped comment must be recorded"
    assert (outcome.get("error") or "").startswith("comment-errors:"), (
        f"a partial comment drop must be a per-mutation error; got {outcome}"
    )
    assert outcome.get("result") == {"key": "DIG-9100", "ok": True}


def test_create_path_comment_drop_promotes_to_error(applier_mod, acli_mod, tmp_path) -> None:
    """CREATE path: a swallowed comment failure during outbound CREATE also promotes to
    outcome["error"] (parity with the update path). The create path has no silent-no-op
    canary, so even a single failed comment is a clean discriminator (RED:
    comment_errors → error=None on unmodified code)."""
    pass_id = f"cr-on-{time.time_ns()}"
    mutation = {
        "direction": "outbound",
        "action": "create",
        "local_id": "create-flagged-comment-fail",
        "fields": {"summary": "a new issue", "issuetype": "Task"},
        "comments": [{"action": "add", "body": "an oversize comment that fails"}],
    }
    client = MagicMock()
    client.create_issue.return_value = {"key": "DIG-9200", "ok": True}
    client.search_issues.return_value = []  # JQL dedup miss so create proceeds
    client.add_comment.side_effect = acli_mod.AcliMutationError(
        "ACLI mutation reported FAILURE (exit=0): comment body too long"
    )
    fake = _make_fake_acli(acli_mod, client)
    with patch.object(applier_mod, "_load_acli", return_value=fake):
        applier_mod.apply([mutation], pass_id, repo_root=tmp_path)
    manifest = scratch_retention.snapshots_dir(tmp_path) / f"{pass_id}.manifest.json"
    outcomes = json.loads(manifest.read_text()).get("mutations", []) if manifest.is_file() else []
    target = [o for o in outcomes if o.get("local_id") == "create-flagged-comment-fail"]
    assert target, f"expected an outcome for the create; got {outcomes}"
    outcome = target[0]
    assert outcome.get("comment_errors"), "the failed create-path comment must be recorded"
    assert (outcome.get("error") or "").startswith("comment-errors:"), (
        f"a create-path comment drop must be a per-mutation error; got {outcome}"
    )


# --------------------------------------------------------------------------- #
# Same-kind silent-no-op canary coverage: comments + labels (was links-only).
# A TOTAL no-op (computed > 0, applied == 0) trips the canary and hard-fails.
# --------------------------------------------------------------------------- #
def test_comments_canary_fires_on_total_noop(applier_mod, acli_mod, tmp_path) -> None:
    """A COMMENTS total no-op (the single comment's add fails: computed=1,
    applied=0) trips the canary — proving comments coverage, not just links. The
    silent-no-op reason augments (never replaces) the comment-errors reason."""
    mutation = {
        "direction": "outbound",
        "action": "update",
        "key": "DIG-9300",
        "fields": {"summary": "scalar ok"},
        "comments": [{"action": "add", "body": "the only comment, and it fails"}],
        "local_id": "loc-9300",
    }
    client = MagicMock()
    client.update_issue.return_value = {"key": "DIG-9300", "ok": True}
    client.add_comment.side_effect = acli_mod.AcliMutationError(
        "ACLI mutation reported FAILURE (exit=0): comment body too long"
    )
    outcome = _apply_one(applier_mod, acli_mod, mutation, client, tmp_path, f"cc-{time.time_ns()}")
    assert outcome.get("comments_applied") == 0
    assert outcome.get("silent_noop") == ["comments"], (
        f"a comments total-no-op must trip the canary; got {outcome.get('silent_noop')}"
    )
    error = outcome.get("error") or ""
    assert error.startswith("comment-errors:"), f"must hard-fail; got {outcome}"
    assert "; silent-noop: " in error, f"both sub-op failures must survive; got {error!r}"


def test_labels_canary_fires_on_total_noop(applier_mod, acli_mod, tmp_path) -> None:
    """A LABELS total no-op (add_label fails: computed=1, applied=0) trips the
    canary — proving labels coverage, not just links — and hard-fails."""
    mutation = {
        "direction": "outbound",
        "action": "update",
        "key": "DIG-9400",
        "fields": {"summary": "scalar ok"},
        "labels": [{"action": "add", "label": "needs-triage"}],
        "local_id": "loc-9400",
    }
    client = MagicMock()
    client.update_issue.return_value = {"key": "DIG-9400", "ok": True}
    client.add_label.side_effect = RuntimeError("label write boom")
    outcome = _apply_one(applier_mod, acli_mod, mutation, client, tmp_path, f"lbl-{time.time_ns()}")
    assert outcome.get("labels_applied") == 0
    assert outcome.get("silent_noop") == ["labels"], (
        f"a labels total-no-op must trip the canary; got {outcome.get('silent_noop')}"
    )
    assert (outcome.get("error") or "").startswith("silent-noop:"), f"must hard-fail; {outcome}"
