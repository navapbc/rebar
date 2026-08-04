"""The DC outbound CREATE posts rebar's own field names to Jira and 400s.

Bug 18a5-2bd8-3e56-4bd8, under epic e369-a449-4773-48fb.

THE DEFECT, proven live rather than reasoned about.

``JiraDataCenterTransport.create_issue`` takes the caller's dict and splats it straight into
``client.create_issue(**fields)``. But ``dispatch_one`` hands it a payload carrying BOTH
schemas: the differ's Jira-shaped keys (``summary``, ``issuetype``, ``status``) AND the
bridge-shaped keys it adds for the Cloud client (``title``, ``ticket_type``), with everything
else passed through untouched. Cloud's ``AcliClient.create_issue`` EXTRACTS the handful of
fields it needs and ignores the rest, so the extra keys are harmless there. Data Center forwards
all of them as Jira field ids, and Jira rejects the request:

    HTTP 400  POST /rest/api/2/issue
      Field 'ticket_type' cannot be set. It is not on the appropriate screen, or unknown.
      Field 'title'       cannot be set. It is not on the appropriate screen, or unknown.
      Field 'status'      cannot be set. It is not on the appropriate screen, or unknown.

The whole create fails, so no issue exists, so no binding is written, and ``get_jira_key``
returns ``None`` — which is how this surfaced, three steps downstream of the fault.

THIS IS THE SEVENTH "CLOUD HAS THE TRANSLATION, DC NEVER GOT ITS HALF" (after d067, 8d68, 751e,
2b16, 88d9, 39c1). ``update_issue``'s own docstring in this module already names the shape for
the status→transition seam: the translation lives PER TRANSPORT. The create path is the same
seam, still missing.

``status`` deserves its own mention: it is not a rejected NAME, it is not settable at create at
all — a Jira status is reached by a workflow transition. Cloud omits it from the create payload
entirely, and so must DC.

WHY THESE ARE UNIT TESTS. The live cell is the acceptance evidence and it costs ~37 minutes per
run. The translation itself is a pure function of the payload, so its boundaries belong here
where they cost milliseconds; the harness proves the create actually binds.
"""

from __future__ import annotations

from typing import Any

import pytest

from rebar_reconciler.adapters.jira_datacenter.transport import JiraDataCenterTransport


class _FakeCreatedIssue:
    def __init__(self, key: str = "RBJ-1") -> None:
        self.raw = {"key": key, "fields": {}}


class _FakeClient:
    """Records the kwargs the transport hands to ``client.create_issue``.

    That call is the whole subject: the bug is not what the transport computes internally, it is
    what reaches Jira. Asserting on the recorded kwargs is asserting on the wire payload.
    """

    def __init__(self) -> None:
        self.create_calls: list[dict[str, Any]] = []

    def create_issue(self, **fields: Any) -> _FakeCreatedIssue:
        self.create_calls.append(dict(fields))
        return _FakeCreatedIssue()

    def fields(self) -> list[dict[str, Any]]:
        return [{"id": "summary", "name": "Summary", "custom": False}]


def _transport(client: _FakeClient) -> JiraDataCenterTransport:
    return JiraDataCenterTransport(client=client, project="RBJ")


# The payload shape `dispatch_one` actually builds: differ keys PLUS bridge keys.
def _dispatch_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "summary": "a real headline",
        "title": "a real headline",
        "issuetype": {"name": "Task"},
        "ticket_type": "task",
        "status": "open",
        "description": "why this exists",
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# HAPPY PATH — the bridge schema is translated into Jira's
# ---------------------------------------------------------------------------


def test_the_bridge_title_becomes_the_jira_summary() -> None:
    """``title`` is the bridge-side name; Jira's is ``summary``."""
    client = _FakeClient()

    _transport(client).create_issue(_dispatch_payload(title="ship the thing"))

    assert len(client.create_calls) == 1
    assert client.create_calls[0].get("summary") == "ship the thing"


def test_the_project_defaults_to_the_transports_project() -> None:
    """Unchanged behaviour, pinned so the translation does not drop it."""
    client = _FakeClient()

    _transport(client).create_issue(_dispatch_payload())

    assert client.create_calls[0].get("project") == {"key": "RBJ"}


# ---------------------------------------------------------------------------
# THE DEFECT — held out from the implementer
# ---------------------------------------------------------------------------


def test_no_bridge_schema_key_is_ever_sent_to_jira() -> None:
    """THE BUG, asserted exactly as Jira reported it.

    ``ticket_type``, ``title`` and ``status`` are the three field names Data Center named in the
    400 that aborted the whole outbound pass. None of them may appear in the create payload.
    """
    client = _FakeClient()

    _transport(client).create_issue(_dispatch_payload())

    sent = client.create_calls[0]
    for forbidden in ("ticket_type", "title", "status"):
        assert forbidden not in sent, (
            f"{forbidden!r} was sent to Jira as a field id. Data Center answers this with "
            f"HTTP 400 'cannot be set. It is not on the appropriate screen, or unknown.' and the "
            f"ENTIRE create fails, so nothing binds. Payload sent: {sent!r}"
        )


def test_status_is_dropped_rather_than_translated() -> None:
    """``status`` has no create-time field form at all — it is a workflow transition.

    So the correct handling is omission, not renaming. A fix that mapped it to some other key
    would still 400; one that kept it under any name would too.
    """
    client = _FakeClient()

    _transport(client).create_issue(_dispatch_payload(status="in_progress"))

    sent = client.create_calls[0]
    assert not any("status" in str(k).lower() for k in sent), (
        f"a status-shaped key survived into the create payload: {sent!r}"
    )


def test_the_ticket_type_becomes_a_jira_issuetype_object() -> None:
    """Jira wants ``issuetype`` as an object with a ``name``, not a bare bridge string."""
    client = _FakeClient()

    _transport(client).create_issue(_dispatch_payload(ticket_type="bug", issuetype=None))

    issuetype = client.create_calls[0].get("issuetype")
    assert isinstance(issuetype, dict), f"issuetype must be an object, got {issuetype!r}"
    assert str(issuetype.get("name", "")).lower() == "bug"


def test_a_jira_shaped_issuetype_still_works() -> None:
    """The differ emits ``issuetype`` as an object already; that path must survive too.

    Both schemas arrive in the same payload, so a translation that only understood one of them
    would work in the harness and break on the other, or vice versa.
    """
    client = _FakeClient()

    _transport(client).create_issue(
        _dispatch_payload(issuetype={"name": "Story"}, ticket_type=None)
    )

    issuetype = client.create_calls[0].get("issuetype")
    assert isinstance(issuetype, dict)
    assert str(issuetype.get("name", "")).lower() == "story"


def test_an_empty_headline_raises_rather_than_creating_a_blank_issue() -> None:
    """Cloud raises here rather than creating an untitled issue, and DC must agree.

    Silently creating a blank issue would bind a local ticket to something unrecognisable, which
    is worse than a loud failure — and this epic's whole subject is silent success.
    """
    client = _FakeClient()

    with pytest.raises(ValueError):
        _transport(client).create_issue(_dispatch_payload(title="", summary=""))

    assert client.create_calls == [], "a create was attempted despite an empty summary"


def test_an_oversize_summary_is_truncated_to_what_data_center_accepts() -> None:
    """DC hard-rejects a summary over 254 characters (measured; see
    ``docs/jira-dc-capability-map.md``). Truncating keeps an over-long title from aborting the
    create, matching how the Cloud client defends the same limit."""
    client = _FakeClient()

    _transport(client).create_issue(_dispatch_payload(title="x" * 400))

    summary = client.create_calls[0]["summary"]
    assert len(summary) <= 254, f"summary is {len(summary)} chars; DC rejects anything over 254"


def test_the_description_survives_the_translation() -> None:
    """A field that is already Jira-shaped must pass through — the fix must not become an
    allowlist so narrow that it drops real content."""
    client = _FakeClient()

    _transport(client).create_issue(_dispatch_payload(description="why this exists"))

    assert client.create_calls[0].get("description") == "why this exists"
