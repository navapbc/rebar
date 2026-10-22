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

from rebar.reconciler import scratch_retention, ticket_planner
from rebar.reconciler.mutation import Mutation, MutationAction, MutationDirection
from rebar.reconciler.mutation_payloads import OutboundCreatePayload, OutboundUpdatePayload

_ACLI_FAIL = "ACLI mutation reported FAILURE (exit=0): comment body too long"

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPTS_DIR = REPO_ROOT / "src" / "rebar" / "_engine"
APPLIER_PATH = SCRIPTS_DIR.parent / "reconciler" / "applier.py"
ACLI_PATH = SCRIPTS_DIR.parent / "reconciler" / "adapters" / "jira" / "acli.py"


def _plans(mutations, *, mode=None, max_changes=None):
    """Ticket plans for the outbound ``mutations``, built as ``run_differs`` builds them.

    The batch form of ``applier.apply`` requires ``ticket_plans`` (ADR 0103 S3). Inbound
    mutations dispatch through ``_apply_typed``, so only the outbound ones are planned.
    """
    _, plans = ticket_planner.plan_pass(
        pass_id="test-pass",
        local_snapshot={},
        remote_snapshot={},
        binding_view={},
        mode=str(getattr(mode, "value", mode) or ""),
        selection={"kind": None, "ids": []},
        limits={"max_changes": max_changes},
        mutations=[
            m
            for m in mutations
            if getattr(getattr(m, "direction", None), "value", None) == "outbound"
        ],
    )
    return plans


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


def _update(key: str, fields: dict, *, local_id: str | None = None, **extra) -> Mutation:
    """A typed outbound update Mutation (``extra``: comments/labels/links tuples)."""
    return Mutation(
        MutationDirection.outbound,
        MutationAction.update,
        key,
        OutboundUpdatePayload(changed_fields=fields, local_id=local_id, **extra),
        {},
    )


def _create(local_id: str, fields: dict, **extra) -> Mutation:
    """A typed outbound create Mutation (``extra``: comments/labels/links tuples)."""
    return Mutation(
        MutationDirection.outbound,
        MutationAction.create,
        local_id,
        OutboundCreatePayload(fields=fields, local_id=local_id, **extra),
        {},
    )


def _apply_one(applier_mod, acli_mod, mutation, client, tmp_path, pass_id) -> dict:
    fake = _make_fake_acli(acli_mod, client)
    with patch.object(applier_mod, "_load_acli", return_value=fake):
        applier_mod.apply([mutation], pass_id, repo_root=tmp_path, ticket_plans=_plans([mutation]))
    manifest = scratch_retention.snapshots_dir(tmp_path) / f"{pass_id}.manifest.json"
    outcomes = json.loads(manifest.read_text()).get("mutations", []) if manifest.is_file() else []
    return next((o for o in outcomes if o.get("key") == mutation.target), {})


def _update_with_partial_comment_drop(acli_mod, *, key: str) -> tuple[Mutation, MagicMock]:
    """A mutation with TWO comments where the FIRST succeeds and the SECOND fails.
    -> comments_computed=2, comments_applied=1 (canary does NOT fire), one
    comment_errors entry. This is the partial-drop gap the canary can't see."""
    mutation = _update(
        key,
        {"summary": "scalar update that succeeds"},
        local_id=f"loc-{key}",
        comments=(
            {"action": "add", "body": "first comment that lands"},
            {"action": "add", "body": "second comment whose add fails"},
        ),
    )
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
    mutation = _create(
        "create-flagged-comment-fail",
        {"summary": "a new issue", "issuetype": "Task"},
        comments=({"action": "add", "body": "an oversize comment that fails"},),
    )
    client = MagicMock()
    client.create_issue.return_value = {"key": "DIG-9200", "ok": True}
    client.search_issues.return_value = []  # JQL dedup miss so create proceeds
    client.add_comment.side_effect = acli_mod.AcliMutationError(
        "ACLI mutation reported FAILURE (exit=0): comment body too long"
    )
    fake = _make_fake_acli(acli_mod, client)
    with patch.object(applier_mod, "_load_acli", return_value=fake):
        applier_mod.apply([mutation], pass_id, repo_root=tmp_path, ticket_plans=_plans([mutation]))
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
    mutation = _update(
        "DIG-9300",
        {"summary": "scalar ok"},
        local_id="loc-9300",
        comments=({"action": "add", "body": "the only comment, and it fails"},),
    )
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
    mutation = _update(
        "DIG-9400",
        {"summary": "scalar ok"},
        local_id="loc-9400",
        labels=({"action": "add", "label": "needs-triage"},),
    )
    client = MagicMock()
    client.update_issue.return_value = {"key": "DIG-9400", "ok": True}
    client.add_label.side_effect = RuntimeError("label write boom")
    outcome = _apply_one(applier_mod, acli_mod, mutation, client, tmp_path, f"lbl-{time.time_ns()}")
    assert outcome.get("labels_applied") == 0
    assert outcome.get("silent_noop") == ["labels"], (
        f"a labels total-no-op must trip the canary; got {outcome.get('silent_noop')}"
    )
    assert (outcome.get("error") or "").startswith("silent-noop:"), f"must hard-fail; {outcome}"
