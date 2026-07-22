"""beer-datum-bark (e534-5154-2401-40fb): a bare RuntimeError from a Jira
transition (acli.transition_issue_by_name — "no transition reaches <status>")
must SOFT-FAIL per-mutation, not abort the whole reconcile pass.

Root cause: the applier's per-mutation isolation is an enumerated allowlist
(400-comment-fallback, 404-stale-binding, AssigneeNotFoundError). The
per-mutation loop in applier._apply_batch only catches HeadDriftError; the bare
RuntimeError from acli.transition_issue_by_name:514-524 is not a JiraAPIError,
so it escapes _apply_one -> _apply_batch and aborts the batch mid-loop, silently
skipping every subsequent mutation.

Intended behavior (design invariant — mirrors the assignee/404/comment soft-fail
pattern): dispatch every mutation the pass can, record the failing one as a
per-mutation outcome error, and continue. The recorded error then counts toward
mutation_failures (reconcile.py:1116-1118), which the pass surfaces as a
non-zero "fail loud" exit (tested separately at the run_pass seam).

RED test: dispatch [good, bad-transition, good]. Pre-fix, apply() raises the
RuntimeError and the third mutation never runs. Post-fix, apply() returns, all
three are attempted, and the manifest records an error only for the bad one.

Mirrors the bootstrap of test_applier_assignee_soft_fail.py.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
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


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def applier_mod() -> ModuleType:
    return _load("applier_transition_softfail", APPLIER_PATH)


@pytest.fixture(scope="module")
def acli_mod() -> ModuleType:
    return _load("acli_transition_softfail", ACLI_PATH)


def _read_manifest_outcomes(manifest_path: Path) -> list[dict]:
    data = json.loads(Path(manifest_path).read_text())
    return data.get("mutations", []) or []


def test_transition_runtime_error_soft_fails_batch_continues(
    applier_mod: ModuleType,
    acli_mod: ModuleType,
    tmp_path: Path,
) -> None:
    """[good, bad-transition, good] in one batch.

    Pre-fix: the bare RuntimeError propagates through apply(), killing the pass;
    the third mutation never runs.

    Post-fix: apply() catches it per-mutation, records an outcome error, and
    continues. All three mutations are attempted; only the bad one has an error.
    """
    good1 = {
        "direction": "outbound",
        "action": "update",
        "key": "DIG-1",
        "fields": {"summary": "a"},
        "local_id": "l1",
    }
    bad = {
        "direction": "outbound",
        "action": "update",
        "key": "DIG-2",
        "fields": {"status": "idea"},
        "local_id": "l2",
    }
    good2 = {
        "direction": "outbound",
        "action": "update",
        "key": "DIG-3",
        "fields": {"summary": "c"},
        "local_id": "l3",
    }

    fake_client = MagicMock()

    def _update_issue_side_effect(issue_key, **kwargs):
        if issue_key == "DIG-2":
            # exactly acli.transition_issue_by_name's raise (acli.py:514-524)
            raise RuntimeError(
                "transition_issue_by_name: no transition reaches 'IDEA' on DIG-2. "
                "Available: ['To Do'->'To Do']"
            )
        return {"key": issue_key, "ok": True}

    fake_client.update_issue.side_effect = _update_issue_side_effect
    # S4: _load_acli returns the transport directly.
    with patch.object(applier_mod, "_load_acli", return_value=fake_client):
        try:
            manifest_path = applier_mod.apply(
                [good1, bad, good2],
                f"test-pass-{int(time.time())}",
                repo_root=tmp_path,
            )
        except RuntimeError as exc:
            pytest.fail(
                f"applier.apply propagated the transition RuntimeError instead of "
                f"soft-failing the batch: {exc!r}"
            )

    # 1) All three mutations were attempted (the good one AFTER the failure ran).
    attempted = [c.args[0] for c in fake_client.update_issue.call_args_list]
    assert attempted == ["DIG-1", "DIG-2", "DIG-3"], (
        f"all three mutations should be attempted in order; got {attempted}"
    )

    # 2) The manifest records an error ONLY for the failed mutation.
    outcomes = _read_manifest_outcomes(manifest_path)
    by_key = {o.get("key"): o for o in outcomes}
    assert set(by_key) == {"DIG-1", "DIG-2", "DIG-3"}, f"missing outcomes: {by_key}"
    assert by_key["DIG-2"].get("error"), "the bad transition must record an outcome error"
    assert not by_key["DIG-1"].get("error"), "the good mutation before must not be an error"
    assert not by_key["DIG-3"].get("error"), "the good mutation after must not be an error"


def test_transition_runtime_error_alone_does_not_raise(
    applier_mod: ModuleType,
    acli_mod: ModuleType,
    tmp_path: Path,
) -> None:
    """Even as the only mutation in the batch, a transition RuntimeError must
    record-and-continue rather than abort."""
    bad = {
        "direction": "outbound",
        "action": "update",
        "key": "DIG-2",
        "fields": {"status": "idea"},
        "local_id": "l2",
    }
    fake_client = MagicMock()
    fake_client.update_issue.side_effect = RuntimeError(
        "transition_issue_by_name: no transition reaches 'IDEA' on DIG-2. Available: [none]"
    )
    # S4: _load_acli returns the transport directly.
    with patch.object(applier_mod, "_load_acli", return_value=fake_client):
        try:
            manifest_path = applier_mod.apply(
                [bad], f"test-pass-solo-{int(time.time())}", repo_root=tmp_path
            )
        except RuntimeError as exc:
            pytest.fail(f"single bad-transition mutation must not raise: {exc!r}")

    outcomes = _read_manifest_outcomes(manifest_path)
    assert any(o.get("key") == "DIG-2" and o.get("error") for o in outcomes), (
        f"expected a recorded error outcome for DIG-2; got {outcomes}"
    )
