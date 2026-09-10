"""Pin Jira Cloud's positive empty-assignee unassign contract (bug 6a74).

The differ sends ``assignee=""``; forwarding that as ``--assignee ""`` silently
does nothing. The transport must instead issue the real Cloud unassign request,
``PUT /assignee`` with ``{"accountId": null}``, and must never place an empty
``--assignee`` flag in ACLI argv. Assertions inspect the resulting REST body and argv,
not merely calls or exceptions, because silent success is the guarded failure mode.
Subprocess, REST, and assignable-search seams are deterministic recorders. This is the
Cloud counterpart to the Data Center positive pins in ``test_dc_unassign_751e.py``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest

from rebar_reconciler.adapters.jira import acli


@dataclass
class _Recorder:
    """Captures every outbound effect the unassign path can have."""

    argvs: list[list[str]] = field(default_factory=list)
    rest_writes: list[tuple[str, str, Any]] = field(default_factory=list)

    def run_acli(self, cmd: list[str], **_kwargs: Any) -> SimpleNamespace:
        self.argvs.append(list(cmd))
        return SimpleNamespace(stdout=json.dumps({"key": cmd[cmd.index("--key") + 1]}))

    @contextmanager
    def urlopen(self, req: Any, **_kwargs: Any) -> Iterator[Any]:
        body = req.data.decode("utf-8") if req.data else ""
        parsed = json.loads(body) if body else None
        self.rest_writes.append((req.get_method(), req.full_url, parsed))
        yield SimpleNamespace(read=lambda: b"")

    @property
    def unassign_bodies(self) -> list[Any]:
        return [body for method, url, body in self.rest_writes if url.endswith("/assignee")]


def _client() -> acli.AcliClient:
    return acli.AcliClient(
        jira_url="https://example.atlassian.net",
        user="u",
        api_token="t",
        jira_project="DIG",
    )


@pytest.fixture
def recorder() -> Iterator[_Recorder]:
    rec = _Recorder()
    with (
        mock.patch.object(acli.acli_subprocess, "_run_acli", rec.run_acli),
        mock.patch.object(acli.AcliClient, "_rest_urlopen_with_retry", rec.urlopen),
    ):
        yield rec


@pytest.mark.parametrize("empty", ["", None])
def test_an_empty_outbound_assignee_actually_unassigns(
    recorder: _Recorder, empty: str | None
) -> None:
    """Both empty sentinels route to the REST unassign, and neither reaches ACLI.

    The assertion is on the REQUEST BODY, not on a stubbed method having been called: the
    thing that makes the fix real is Cloud's ``{"accountId": null}`` sentinel landing on
    ``/rest/api/3/issue/<key>/assignee``.
    """
    _client().update_issue("DIG-1", assignee=empty)

    assert recorder.unassign_bodies == [{"accountId": None}], (
        "an empty assignee must PUT Cloud's unassign sentinel to the /assignee endpoint, "
        f"got REST writes {recorder.rest_writes!r}"
    )
    assert recorder.argvs == [], (
        "with the assignee popped no editable field remains, so no ACLI subprocess may be "
        f'spawned -- and `--assignee ""` must never be sent. Got {recorder.argvs!r}'
    )


def test_unassign_co_submitted_with_an_editable_field_does_both(recorder: _Recorder) -> None:
    """A mutation that clears the assignee AND edits a field must apply both halves.

    The DC sibling of this test guards the naive fix that swallows the whole update once it
    recognises the unassign; the Cloud equivalent is dropping the ``workitem edit`` argv.
    """
    _client().update_issue("DIG-1", assignee="", summary="new summary")

    assert recorder.unassign_bodies == [{"accountId": None}], (
        f"the unassign half must still fire, got {recorder.rest_writes!r}"
    )
    assert len(recorder.argvs) == 1, (
        f"the editable half must still be dispatched exactly once, got {recorder.argvs!r}"
    )
    argv = recorder.argvs[0]
    assert argv[:3] == ["jira", "workitem", "edit"]
    assert "--summary" in argv and "new summary" in argv, (
        f"the co-submitted field edit must survive the unassign, got argv={argv!r}"
    )
    assert "--assignee" not in argv, (
        "the empty assignee must never reach ACLI, where it silently no-ops rather than "
        f"unassigning. Got argv={argv!r}"
    )


def test_a_non_empty_assignee_still_takes_the_validate_then_edit_path(
    recorder: _Recorder,
) -> None:
    """The contrast case: the normal assign path is untouched by the empty-assignee branch."""
    client = _client()
    # PT008 is excluded here: it would swap this two-arg lambda for a
    # `return_value=` MagicMock, which accepts ANY call shape. The lambda pins
    # production to calling `_direct_rest_get(self, path)` with exactly one path.
    with mock.patch.object(  # noqa: PT008
        acli.AcliClient,
        "_direct_rest_get",
        lambda _self, _path: [
            {"accountId": "abc123", "emailAddress": "joe@example.com", "displayName": "Joe"}
        ],
    ):
        client.update_issue("DIG-1", assignee="joe@example.com", summary="new summary")

    assert recorder.unassign_bodies == [], (
        f"a real assignee must never be routed to unassign, got {recorder.rest_writes!r}"
    )
    argv = recorder.argvs[0]
    assert "--assignee" in argv, f"a real assignee must be submitted to ACLI, got argv={argv!r}"
    assert "abc123" in argv, (
        f"the assignee must be normalised to the matched accountId, got argv={argv!r}"
    )
