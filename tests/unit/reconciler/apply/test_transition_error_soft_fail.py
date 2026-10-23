"""A bare RuntimeError from an unreachable Jira transition is isolated per mutation. The
batch continues and records the precise failure instead of aborting later writes."""

from __future__ import annotations

import importlib
import json
import time
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

from rebar.reconciler import ticket_planner
from rebar.reconciler.mutation import Mutation, MutationAction, MutationDirection
from rebar.reconciler.mutation_payloads import OutboundUpdatePayload

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPTS_DIR = REPO_ROOT / "src" / "rebar" / "_engine"
APPLIER_PATH = SCRIPTS_DIR.parent / "reconciler" / "applier.py"
ACLI_PATH = SCRIPTS_DIR.parent / "reconciler" / "adapters" / "jira" / "acli.py"

_ADF_PATH = SCRIPTS_DIR.parent / "reconciler" / "adapters" / "jira" / "adf.py"
_CL_PATH = SCRIPTS_DIR.parent / "reconciler" / "adapters" / "jira" / "comment_limits.py"


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
def applier_mod() -> ModuleType:
    return importlib.import_module("rebar.reconciler.applier")


@pytest.fixture(scope="module")
def acli_mod() -> ModuleType:
    return importlib.import_module("rebar.reconciler.adapters.jira.acli")


def _update(key: str, fields: dict, *, local_id: str | None = None, **extra) -> Mutation:
    """A typed outbound update Mutation (``extra``: comments/labels/links tuples)."""
    return Mutation(
        MutationDirection.outbound,
        MutationAction.update,
        key,
        OutboundUpdatePayload(changed_fields=fields, local_id=local_id, **extra),
        {},
    )


def _read_manifest_outcomes(manifest_path: Path) -> list[dict]:
    data = json.loads(Path(manifest_path).read_text())
    return data.get("mutations", []) or []


def test_transition_runtime_error_soft_fails_batch_continues(
    applier_mod: ModuleType,
    acli_mod: ModuleType,
    tmp_path: Path,
) -> None:
    """In a good/bad/good batch, record the transition error on the bad mutation and still
    attempt both good writes."""
    good1 = _update("DIG-1", {"summary": "a"}, local_id="l1")
    bad = _update("DIG-2", {"status": "idea"}, local_id="l2")
    good2 = _update("DIG-3", {"summary": "c"}, local_id="l3")

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
                ticket_plans=_plans([good1, bad, good2]),
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
    bad = _update("DIG-2", {"status": "idea"}, local_id="l2")
    fake_client = MagicMock()
    fake_client.update_issue.side_effect = RuntimeError(
        "transition_issue_by_name: no transition reaches 'IDEA' on DIG-2. Available: [none]"
    )
    # S4: _load_acli returns the transport directly.
    with patch.object(applier_mod, "_load_acli", return_value=fake_client):
        try:
            manifest_path = applier_mod.apply(
                [bad],
                f"test-pass-solo-{int(time.time())}",
                repo_root=tmp_path,
                ticket_plans=_plans([bad]),
            )
        except RuntimeError as exc:
            pytest.fail(f"single bad-transition mutation must not raise: {exc!r}")

    outcomes = _read_manifest_outcomes(manifest_path)
    assert any(o.get("key") == "DIG-2" and o.get("error") for o in outcomes), (
        f"expected a recorded error outcome for DIG-2; got {outcomes}"
    )
