"""Cloud outbound status must use a transition, never workitem edit.

Pin both the positive transition call and the absence of status from edit
fields. Record transition and ACLI arguments without spawning a subprocess or
calling the network. This complements the Data Center routing check."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest

from rebar.reconciler.adapters.jira import acli


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
