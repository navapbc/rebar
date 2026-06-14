"""Axis 3 — surface the swallowed add_comment failure in the batch outcome.

Bug 6afc-20ee-84e5-4dd5. When ``client.add_comment`` raises (e.g. ACLI's
silent exit-0 over-length failure surfaced as ``AcliMutationError``), the
applier's update_one comment-dispatch loop CAUGHT the exception and only logged
it — the batch outcome stayed ``error=None``. As a result ``RECON: batch_outcome``
reported success while the comment never landed, hiding the divergence that
drives the outbound comment-sync loop.

Fix: when add_comment raises, collect it and propagate into the per-mutation
outcome (a ``comment_errors`` field) so the manifest no longer reports a clean
outcome for a mutation whose comment sub-mutation failed. The failure stays
NON-fatal — the scalar field update genuinely succeeded — matching the existing
stale-binding-404 / assignee-unresolved soft-fail style.

RED test: an update mutation that carries a comment whose add_comment raises
must produce an outcome whose ``comment_errors`` is populated (not a clean
outcome), while the scalar update still succeeds and the batch does not abort.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPTS_DIR = REPO_ROOT / "src" / "rebar" / "_engine"
APPLIER_PATH = SCRIPTS_DIR / "rebar_reconciler" / "applier.py"
ACLI_PATH = SCRIPTS_DIR / "rebar_reconciler" / "acli.py"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
_ADF_PATH = SCRIPTS_DIR / "rebar_reconciler" / "adf.py"
if "rebar_reconciler" not in sys.modules:
    import types as _types

    _dr = _types.ModuleType("rebar_reconciler")
    _dr.__path__ = [str(SCRIPTS_DIR / "rebar_reconciler")]
    sys.modules["rebar_reconciler"] = _dr
if "rebar_reconciler.adf" not in sys.modules:
    _adf_spec = importlib.util.spec_from_file_location("rebar_reconciler.adf", _ADF_PATH)
    _adf_mod = importlib.util.module_from_spec(_adf_spec)
    sys.modules["rebar_reconciler.adf"] = _adf_mod
    _adf_spec.loader.exec_module(_adf_mod)  # type: ignore[union-attr]
_CL_PATH = SCRIPTS_DIR / "rebar_reconciler" / "comment_limits.py"
if "rebar_reconciler.comment_limits" not in sys.modules:
    _cl_spec = importlib.util.spec_from_file_location("rebar_reconciler.comment_limits", _CL_PATH)
    _cl_mod = importlib.util.module_from_spec(_cl_spec)
    sys.modules["rebar_reconciler.comment_limits"] = _cl_mod
    _cl_spec.loader.exec_module(_cl_mod)  # type: ignore[union-attr]


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def applier_mod() -> Iterator[ModuleType]:
    name = "applier_comment_error_surfaced"
    mod = _load_module(name, APPLIER_PATH)
    try:
        yield mod
    finally:
        sys.modules.pop(name, None)


@pytest.fixture(scope="module")
def acli_mod() -> Iterator[ModuleType]:
    name = "acli_comment_error_surfaced"
    mod = _load_module(name, ACLI_PATH)
    try:
        yield mod
    finally:
        sys.modules.pop(name, None)


def _make_fake_acli(acli_mod: ModuleType, client: MagicMock) -> MagicMock:
    fake = MagicMock()
    fake.AcliClient.return_value = client
    fake.AssigneeNotFoundError = acli_mod.AssigneeNotFoundError
    return fake


def _read_manifest_outcomes(repo_root: Path, pass_id: str) -> list[dict]:
    manifest_path = repo_root / "bridge_state" / "snapshots" / f"{pass_id}.manifest.json"
    if not manifest_path.is_file():
        return []
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    return data.get("mutations", [])


def test_add_comment_failure_surfaces_in_outcome(
    applier_mod: ModuleType,
    acli_mod: ModuleType,
    tmp_path: Path,
) -> None:
    """An update whose add_comment raises must surface a comment_errors record
    in the batch outcome — not a clean error=None outcome — while the scalar
    update succeeds and the batch does not abort."""
    pass_id = f"test-pass-comment-err-{int(time.time())}"

    mutation = {
        "direction": "outbound",
        "action": "update",
        "key": "DIG-8000",
        "fields": {"summary": "scalar update that succeeds"},
        "comments": [{"action": "add", "body": "an oversize comment that fails"}],
        "local_id": "comment-fail-id",
    }

    fake_client = MagicMock()
    fake_client.update_issue.return_value = {"key": "DIG-8000", "ok": True}
    fake_client.add_comment.side_effect = acli_mod.AcliMutationError(
        "ACLI mutation reported FAILURE (exit=0): comment body too long"
    )
    fake_acli_mod = _make_fake_acli(acli_mod, fake_client)

    with patch.object(applier_mod, "_load_acli", return_value=fake_acli_mod):
        # Non-fatal: apply() must not raise on a failed comment sub-mutation.
        applier_mod.apply([mutation], pass_id, repo_root=tmp_path)

    # The scalar update was actually attempted.
    assert fake_client.update_issue.called, "scalar update must still be attempted"
    assert fake_client.add_comment.called, "add_comment must have been attempted"

    outcomes = _read_manifest_outcomes(tmp_path, pass_id)
    target_outcomes = [o for o in outcomes if o.get("key") == "DIG-8000"]
    assert target_outcomes, f"expected an outcome for DIG-8000; got {outcomes}"
    outcome = target_outcomes[0]

    comment_errors = outcome.get("comment_errors")
    assert comment_errors, (
        "A failed add_comment sub-mutation must surface in the outcome's "
        f"comment_errors field; got outcome: {outcome}"
    )
    assert any("too long" in str(e) or "FAILURE" in str(e) for e in comment_errors), (
        f"comment_errors must name the underlying failure; got {comment_errors}"
    )

    # The scalar field update still succeeded (the failure is non-fatal).
    assert outcome.get("result") == {"key": "DIG-8000", "ok": True}, (
        "the scalar update genuinely succeeded; its result must be recorded "
        f"even though a comment sub-mutation failed; got {outcome}"
    )


def test_successful_comment_leaves_no_comment_errors(
    applier_mod: ModuleType,
    acli_mod: ModuleType,
    tmp_path: Path,
) -> None:
    """When add_comment succeeds, no comment_errors record is produced (the new
    field must not fire spuriously)."""
    pass_id = f"test-pass-comment-ok-{int(time.time())}"
    mutation = {
        "direction": "outbound",
        "action": "update",
        "key": "DIG-8001",
        "fields": {"summary": "ok"},
        "comments": [{"action": "add", "body": "a normal comment"}],
        "local_id": "comment-ok-id",
    }
    fake_client = MagicMock()
    fake_client.update_issue.return_value = {"key": "DIG-8001", "ok": True}
    fake_client.add_comment.return_value = {"id": "1", "body": "a normal comment"}
    fake_acli_mod = _make_fake_acli(acli_mod, fake_client)

    with patch.object(applier_mod, "_load_acli", return_value=fake_acli_mod):
        applier_mod.apply([mutation], pass_id, repo_root=tmp_path)

    outcomes = _read_manifest_outcomes(tmp_path, pass_id)
    target = [o for o in outcomes if o.get("key") == "DIG-8001"]
    assert target, f"expected an outcome for DIG-8001; got {outcomes}"
    assert not target[0].get("comment_errors"), (
        f"successful comment must not populate comment_errors; got {target[0]}"
    )
