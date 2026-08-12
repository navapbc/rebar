"""The OUTBOUND status must never reach the Cloud ``workitem edit`` field set (bug 6194).

PARITY GUARD. This is the Cloud mirror of the Data Center pin
``test_dc_outbound_status_routing_5200.py::test_the_dc_transport_never_edits_status_as_a_field``.
DC guards the negative half — no ``status`` key in the REST field-edit payload — because
``status`` is NOT an editable Jira field; it is only reachable through a transition. Cloud has
always had the correct routing (``adapters/jira/acli.py::update_issue`` pops ``status`` out of
kwargs and hands it to ``transition_issue`` before building the ``jira workitem edit`` argv),
but nothing pinned it, so the DC-side guarantee had no Cloud counterpart.

WHY THE NEGATIVE HALF IS WORTH ITS OWN TEST. ``test_update_one_forwards_status_to_client``
already covers the dispatch layer forwarding ``status`` to the transport, and the transition
NAME resolution is pinned by ``test_acli_status_resolution_heldout.py``. Neither would fail a
partial regression that routed the transition correctly but ALSO left ``status`` in the edit
kwargs — Jira would reject that edit on every pass, exactly the live failure mode story 5200
diagnosed on DC. That is the gap this file closes.

DETERMINISM. Nothing here spawns a subprocess or touches the network: ``transition_issue`` is
replaced by a recorder (its own resolution is another file's contract) and the ACLI subprocess
seam ``acli_subprocess._run_acli`` is replaced by an argv recorder returning canned JSON — the
same stubbing shape as ``test_acli_status_resolution_heldout.py``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest

from rebar_reconciler.adapters.jira import acli


@dataclass
class _Recorder:
    """Captures both halves of the outbound status path."""

    argvs: list[list[str]] = field(default_factory=list)
    transitions: list[tuple[str, str]] = field(default_factory=list)

    def run_acli(self, cmd: list[str], **_kwargs: Any) -> SimpleNamespace:
        self.argvs.append(list(cmd))
        return SimpleNamespace(stdout=json.dumps({"key": cmd[cmd.index("--key") + 1]}))

    def transition_issue(self, jira_key: str, status: str) -> dict[str, str]:
        self.transitions.append((jira_key, status))
        return {"key": jira_key, "status": status}


@pytest.fixture
def recorder() -> Iterator[_Recorder]:
    rec = _Recorder()
    with (
        mock.patch.object(acli.acli_subprocess, "_run_acli", rec.run_acli),
        mock.patch.object(acli, "transition_issue", rec.transition_issue),
    ):
        yield rec


def _edit_argv(recorder: _Recorder) -> list[str]:
    """The single ``workitem edit`` argv, asserting exactly one was built."""
    assert len(recorder.argvs) == 1, f"expected exactly one ACLI invocation, got {recorder.argvs!r}"
    argv = recorder.argvs[0]
    assert argv[:3] == ["jira", "workitem", "edit"], f"unexpected ACLI verb: {argv[:3]!r}"
    return argv


def test_status_never_appears_in_the_workitem_edit_field_set(recorder: _Recorder) -> None:
    """The negative half, mirroring the DC pin: ``--status`` must never be emitted.

    A co-submitted editable field is included so the argv is actually built — the failure
    this guards is ``status`` riding ALONG with the real fields, not replacing them.
    """
    acli.update_issue("PROJ-1", status="in_progress", summary="s")

    argv = _edit_argv(recorder)
    assert "--status" not in argv, (
        "`status` is not an editable Jira field, so it must never reach the `workitem edit` "
        f"field set. Got argv={argv!r}"
    )
    assert not any(arg.startswith("--") and "status" in arg for arg in argv), (
        f"no ACLI flag may carry the status field at all. Got argv={argv!r}"
    )
    assert "--summary" in argv and "s" in argv, (
        f"the co-submitted editable field must still be edited. Got argv={argv!r}"
    )


def test_status_is_dispatched_as_a_transition_not_dropped(recorder: _Recorder) -> None:
    """The positive half: dropping status on the floor must NOT satisfy the guard above."""
    acli.update_issue("PROJ-1", status="in_progress", summary="s")

    assert recorder.transitions == [("PROJ-1", "in_progress")], (
        "the outbound status must be dispatched exactly once through `transition_issue`, "
        f"got {recorder.transitions!r}"
    )


def test_a_status_only_update_spawns_no_workitem_edit_at_all(recorder: _Recorder) -> None:
    """With status as the ONLY kwarg there is no editable field left, so there is nothing to edit.

    The DC sibling asserts ``edited_fields == []`` for the same reason; on Cloud the observable
    equivalent is that no ACLI subprocess is built at all.
    """
    result = acli.update_issue("PROJ-1", status="closed")

    assert recorder.argvs == [], (
        f"a status-only update must not invoke ACLI at all, got {recorder.argvs!r}"
    )
    assert recorder.transitions == [("PROJ-1", "closed")]
    assert result == {"key": "PROJ-1", "status": "closed"}, (
        f"the contractual return value must report the routed status, got {result!r}"
    )
