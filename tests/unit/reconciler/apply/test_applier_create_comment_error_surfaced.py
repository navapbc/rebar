"""Verify create comment failures are visible and nonfatal.

When `add_comment` raises, the create outcome carries `comment_errors`.
The issue creation still succeeds and the batch continues, so a missing
comment cannot appear as a clean outcome.
"""

from __future__ import annotations

import importlib
import json
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

from rebar.reconciler import scratch_retention
from rebar.reconciler.mutation import Mutation, MutationAction, MutationDirection
from rebar.reconciler.mutation_payloads import OutboundCreatePayload

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPTS_DIR = REPO_ROOT / "src" / "rebar" / "_engine"
APPLIER_PATH = SCRIPTS_DIR.parent / "reconciler" / "applier.py"
ACLI_PATH = SCRIPTS_DIR.parent / "reconciler" / "adapters" / "jira" / "acli.py"

_ADF_PATH = SCRIPTS_DIR.parent / "reconciler" / "adapters" / "jira" / "adf.py"
_CL_PATH = SCRIPTS_DIR.parent / "reconciler" / "adapters" / "jira" / "comment_limits.py"


@pytest.fixture(scope="module")
def applier_mod() -> Iterator[ModuleType]:
    name = "applier_create_comment_error_surfaced"
    mod = importlib.import_module("rebar.reconciler.applier")
    try:
        yield mod
    finally:
        sys.modules.pop(name, None)


@pytest.fixture(scope="module")
def acli_mod() -> Iterator[ModuleType]:
    name = "acli_create_comment_error_surfaced"
    mod = importlib.import_module("rebar.reconciler.adapters.jira.acli")
    try:
        yield mod
    finally:
        sys.modules.pop(name, None)


def _make_fake_acli(acli_mod: ModuleType, client: MagicMock) -> MagicMock:
    # S4: _load_acli returns the transport directly.
    return client


def _read_manifest_outcomes(repo_root: Path, pass_id: str) -> list[dict]:
    manifest_path = scratch_retention.snapshots_dir(repo_root) / f"{pass_id}.manifest.json"
    if not manifest_path.is_file():
        return []
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    return data.get("mutations", [])


def _create(local_id: str, fields: dict, **extra) -> Mutation:
    """A typed outbound create Mutation (``extra``: comments/labels/links tuples)."""
    return Mutation(
        MutationDirection.outbound,
        MutationAction.create,
        local_id,
        OutboundCreatePayload(fields=fields, local_id=local_id, **extra),
        {},
    )


def test_create_add_comment_failure_surfaces_in_outcome(
    applier_mod: ModuleType,
    acli_mod: ModuleType,
    tmp_path: Path,
) -> None:
    """An outbound CREATE whose add_comment raises must surface a comment_errors
    record in the batch outcome — not a clean error=None outcome — while the
    issue create succeeds and the batch does not abort."""
    pass_id = f"test-pass-create-comment-err-{int(time.time())}"

    mutation = _create(
        "create-comment-fail-id",
        {"summary": "a new issue", "issuetype": "Task"},
        comments=({"action": "add", "body": "an oversize comment that fails"},),
    )

    fake_client = MagicMock()
    fake_client.create_issue.return_value = {"key": "DIG-9000", "ok": True}
    # JQL dedup miss so create proceeds.
    fake_client.search_issues.return_value = []
    fake_client.add_comment.side_effect = acli_mod.AcliMutationError(
        "ACLI mutation reported FAILURE (exit=0): comment body too long"
    )
    fake_acli_mod = _make_fake_acli(acli_mod, fake_client)

    with patch.object(applier_mod, "_load_acli", return_value=fake_acli_mod):
        # Non-fatal: apply() must not raise on a failed comment sub-mutation.
        applier_mod.apply([mutation], pass_id, repo_root=tmp_path)

    # The issue create was actually attempted, and the comment dispatch ran.
    assert fake_client.create_issue.called, "issue create must still be attempted"
    assert fake_client.add_comment.called, "add_comment must have been attempted"

    outcomes = _read_manifest_outcomes(tmp_path, pass_id)
    target_outcomes = [o for o in outcomes if o.get("local_id") == "create-comment-fail-id"]
    assert target_outcomes, f"expected an outcome for the create; got {outcomes}"
    outcome = target_outcomes[0]

    comment_errors = outcome.get("comment_errors")
    assert comment_errors, (
        "A failed add_comment sub-mutation during CREATE must surface in the "
        f"outcome's comment_errors field; got outcome: {outcome}"
    )
    assert any("too long" in str(e) or "FAILURE" in str(e) for e in comment_errors), (
        f"comment_errors must name the underlying failure; got {comment_errors}"
    )

    # The issue create still succeeded (the failure is non-fatal).
    assert outcome.get("result") == {"key": "DIG-9000", "ok": True}, (
        "the issue create genuinely succeeded; its result must be recorded "
        f"even though a comment sub-mutation failed; got {outcome}"
    )


def test_create_successful_comment_leaves_no_comment_errors(
    applier_mod: ModuleType,
    acli_mod: ModuleType,
    tmp_path: Path,
) -> None:
    """When add_comment succeeds during CREATE, no comment_errors record is
    produced (the new field must not fire spuriously)."""
    pass_id = f"test-pass-create-comment-ok-{int(time.time())}"
    mutation = _create(
        "create-comment-ok-id",
        {"summary": "ok", "issuetype": "Task"},
        comments=({"action": "add", "body": "a normal comment"},),
    )
    fake_client = MagicMock()
    fake_client.create_issue.return_value = {"key": "DIG-9001", "ok": True}
    fake_client.search_issues.return_value = []
    fake_client.add_comment.return_value = {"id": "1", "body": "a normal comment"}
    fake_acli_mod = _make_fake_acli(acli_mod, fake_client)

    with patch.object(applier_mod, "_load_acli", return_value=fake_acli_mod):
        applier_mod.apply([mutation], pass_id, repo_root=tmp_path)

    outcomes = _read_manifest_outcomes(tmp_path, pass_id)
    target = [o for o in outcomes if o.get("local_id") == "create-comment-ok-id"]
    assert target, f"expected an outcome for the create; got {outcomes}"
    assert not target[0].get("comment_errors"), (
        f"successful comment must not populate comment_errors; got {target[0]}"
    )
