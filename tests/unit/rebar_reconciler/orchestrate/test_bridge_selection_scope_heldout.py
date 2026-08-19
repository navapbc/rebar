"""Held-out oracle for pre-differ canonical selection scope."""

from __future__ import annotations

import enum
import json
import types
from unittest.mock import patch

import pytest

from rebar_reconciler import reconcile


class _Bindings:
    def __init__(self) -> None:
        self._local_to_jira = {"alpha": "DIG-1", "beta": "DIG-2"}

    def get_jira_key(self, local_id: str) -> str | None:
        return self._local_to_jira.get(local_id)

    # -- write-boundary interface (RP-02 S3 T2, flamboyant-possessive-blackbuck): the
    #    load phase asks every store to repair interrupted retirements once per pass, so
    #    the fake must answer that call. A no-op is the RIGHT answer here and not a
    #    shortcut: this double has no retired state at all, so a real store built from it
    #    would classify no candidates and write nothing either. -------------------------
    def repair_at_write_boundary(self, *, persist: bool, scoped: bool) -> None:
        return None


class _Mode(enum.Enum):
    PREVIEW = "dry-run"


_LOCAL_TICKETS = [
    {"ticket_id": "alpha", "title": "selected"},
    {"ticket_id": "beta", "title": "control"},
]
_SNAPSHOT = {"DIG-1": {"summary": "selected"}, "DIG-2": {"summary": "control"}}


def _module_loader(differ):
    modules = {
        "fetcher.py": types.SimpleNamespace(
            compute_snapshot=lambda _pass_id, _repo_root: dict(_SNAPSHOT)
        ),
        "binding_store.py": types.SimpleNamespace(
            load_binding_store=lambda _repo_root: _Bindings()
        ),
        "mode.py": types.SimpleNamespace(MODE_CAPS={_Mode.PREVIEW: 0}),
        "run_differs.py": differ,
    }

    def load_module(_name: str, filename: str):
        return modules.get(filename, types.SimpleNamespace())

    return load_module


def _seed_previous_snapshot(tmp_path) -> None:
    previous = tmp_path / ".tickets-tracker" / ".bridge_state" / "prev_snapshot.json"
    previous.parent.mkdir(parents=True)
    previous.write_text(json.dumps(_SNAPSHOT), encoding="utf-8")


@pytest.mark.parametrize(
    ("selection_kwargs", "expected_local", "expected_jira"),
    [
        (
            {"selection_kind": "only", "selection_ids": {"alpha"}},
            ["alpha"],
            ["DIG-1"],
        ),
        (
            {"selection_kind": "except", "selection_ids": {"alpha"}},
            ["beta"],
            ["DIG-2"],
        ),
    ],
)
def test_canonical_selection_narrows_every_differ_input_before_differ(
    tmp_path, selection_kwargs: dict, expected_local: list[str], expected_jira: list[str]
) -> None:
    observed: dict = {}
    differ = types.SimpleNamespace()

    def run_differs(ctx) -> None:
        observed["local"] = [item["ticket_id"] for item in ctx.local_tickets]
        observed["prev"] = sorted(ctx.prev_snapshot)
        observed["curr"] = sorted(ctx.curr_snapshot)
        ctx.mutations = []

    differ.run_differs = run_differs
    _seed_previous_snapshot(tmp_path)

    with (
        patch.object(reconcile, "_read_local_tickets", return_value=list(_LOCAL_TICKETS)),
        patch.object(reconcile, "_load", side_effect=_module_loader(differ)),
        patch.object(reconcile, "_apply_mutations"),
        patch.object(reconcile, "_persist_and_log", return_value={"pass_id": "scope"}),
    ):
        result = reconcile.reconcile_once(
            "scope",
            repo_root=tmp_path,
            target_mode=_Mode.PREVIEW,
            **selection_kwargs,
        )

    assert result == {"pass_id": "scope"}
    assert observed == {
        "local": expected_local,
        "prev": expected_jira,
        "curr": expected_jira,
    }


def test_disappeared_preflight_selection_stops_before_differ_or_apply(tmp_path) -> None:
    differ = types.SimpleNamespace(
        run_differs=lambda _ctx: pytest.fail("differ must not run for a disappeared selection")
    )

    with (
        patch.object(reconcile, "_read_local_tickets", return_value=list(_LOCAL_TICKETS)),
        patch.object(reconcile, "_load", side_effect=_module_loader(differ)),
        patch.object(reconcile, "_apply_mutations") as apply_spy,
        patch.object(reconcile, "_persist_and_log") as persist_spy,
        pytest.raises(ValueError, match="vanished"),
    ):
        reconcile.reconcile_once(
            "scope-missing",
            repo_root=tmp_path,
            target_mode=_Mode.PREVIEW,
            selection_kind="only",
            selection_ids={"vanished"},
        )

    apply_spy.assert_not_called()
    persist_spy.assert_not_called()
