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


def test_priority_is_wrapped_in_the_object_jira_rest_requires() -> None:
    """SECOND LAYER, found by a live run rather than by reasoning.

    With the bridge-schema names fixed, the create got FURTHER and failed differently:

        "errors":{"priority":"Could not find valid 'id' or 'name' in priority object."}

    The shared outbound mapper already turns rebar's integer priority into a Jira NAME
    (``LOCAL_PRIORITY_TO_JIRA`` -> ``"Medium"``), so a bare string arrives here. Jira's REST API
    wants an OBJECT. ACLI accepts the bare name, which is why Cloud never needed this — the same
    per-transport seam as the rest of this bug.
    """
    client = _FakeClient()

    _transport(client).create_issue(_dispatch_payload(priority="Medium"))

    assert client.create_calls[0].get("priority") == {"name": "Medium"}, (
        f"priority reached Jira unwrapped: {client.create_calls[0].get('priority')!r}. Data "
        f"Center answers that with 400 \"Could not find valid 'id' or 'name' in priority "
        f'object" and the whole create fails.'
    )


def test_assignee_is_wrapped_in_the_object_jira_rest_requires() -> None:
    """The other half of the same 400: ``"assignee":"data was not an object"``.

    Data Center identifies users by ``name`` (never Cloud's accountId — that is why
    ``validate_assignee_exists`` returns the username on this transport), so the resolved handle
    must be wrapped rather than sent bare.
    """
    client = _FakeClient()

    _transport(client).create_issue(_dispatch_payload(assignee="jdoe"))

    assert client.create_calls[0].get("assignee") == {"name": "jdoe"}, (
        f"assignee reached Jira unwrapped: {client.create_calls[0].get('assignee')!r}. Data "
        f'Center answers that with 400 "data was not an object".'
    )


def test_an_already_shaped_priority_or_assignee_is_left_alone() -> None:
    """Wrapping must be idempotent in shape: a caller that already passed the object form must
    not end up with ``{"name": {"name": ...}}``, which would 400 for a third distinct reason."""
    client = _FakeClient()

    _transport(client).create_issue(
        _dispatch_payload(priority={"id": "3"}, assignee={"name": "jdoe"})
    )

    sent = client.create_calls[0]
    assert sent.get("priority") == {"id": "3"}, f"priority was re-wrapped: {sent.get('priority')!r}"
    assert sent.get("assignee") == {"name": "jdoe"}, (
        f"assignee was re-wrapped: {sent.get('assignee')!r}"
    )


def test_absent_priority_and_assignee_are_not_invented() -> None:
    """A create that names neither must not acquire them.

    Injecting an empty or default object would either 400 or, worse, silently assign the issue
    to nobody-in-particular — the create equivalent of the mis-assignment bug 544e records on the
    Cloud side.
    """
    client = _FakeClient()
    payload = _dispatch_payload()
    payload.pop("priority", None)
    payload.pop("assignee", None)

    _transport(client).create_issue(payload)

    sent = client.create_calls[0]
    assert "priority" not in sent, f"priority was invented: {sent.get('priority')!r}"
    assert "assignee" not in sent, f"assignee was invented: {sent.get('assignee')!r}"


def test_a_null_valued_optional_is_dropped_rather_than_sent_as_null() -> None:
    """THIRD LAYER, again found by a live run rather than predicted.

    With priority wrapped, the next 400 was ``"assignee":"data was not an object"`` — and the
    value was not a string at all, it was ``None``. The differ emits the key with a null when the
    ticket has no assignee, and the previous wrap only fired for a non-empty string, so the null
    passed straight through. Jira reads a null where it expects an object and rejects the whole
    create.

    Dropping is right rather than wrapping: there is no object that means "unassigned" at create
    time, and inventing one would assign the issue to somebody.
    """
    client = _FakeClient()

    _transport(client).create_issue(_dispatch_payload(assignee=None, priority=None))

    sent = client.create_calls[0]
    assert "assignee" not in sent, f"a null assignee was sent to Jira: {sent!r}"
    assert "priority" not in sent, f"a null priority was sent to Jira: {sent!r}"


def test_the_parent_is_wrapped_as_a_key_object() -> None:
    """``parent`` is the other half of the third layer, and it wraps DIFFERENTLY.

    Jira identifies a parent by ``key``, not by ``name`` — ``{"key": "RBJ-1"}``. A translation
    that reused the ``{"name": ...}`` shape used for priority and assignee would still 400, so
    this cell pins the distinction rather than assuming the wrapper is uniform.
    """
    client = _FakeClient()

    _transport(client).create_issue(_dispatch_payload(parent="RBJ-7"))

    assert client.create_calls[0].get("parent") == {"key": "RBJ-7"}, (
        f"parent reached Jira as {client.create_calls[0].get('parent')!r}; Data Center answers a "
        f'bare value with 400 "data was not an object".'
    )


def test_an_already_shaped_parent_is_left_alone() -> None:
    """The differ may already emit the object form; it must not be nested a second time."""
    client = _FakeClient()

    _transport(client).create_issue(_dispatch_payload(parent={"key": "RBJ-7"}))

    assert client.create_calls[0].get("parent") == {"key": "RBJ-7"}


def test_the_description_survives_the_translation() -> None:
    """A field that is already Jira-shaped must pass through — the fix must not become an
    allowlist so narrow that it drops real content."""
    client = _FakeClient()

    _transport(client).create_issue(_dispatch_payload(description="why this exists"))

    assert client.create_calls[0].get("description") == "why this exists"
