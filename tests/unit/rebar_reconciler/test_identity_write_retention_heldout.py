"""HELD-OUT: a failed identity write must NOT delete the created issue (bug 387d).

THE DEFECT. `create_one` writes identity markers straight after `create_issue` succeeds, and
on ANY failure in that block it DELETED the freshly created issue and re-raised. Destroying a
successfully-created issue to recover from a failure to LABEL it inverts the cost: a
created-but-unlabelled issue is recoverable, a deleted one is not.

The trigger is ordinary, not exotic. Jira rejects a field write with "Field 'x' cannot be set.
It is not on the appropriate screen, or unknown" in at least three distinct situations, and the
message is misleading in roughly half of them: the field genuinely is not on the project's
Create/Edit screen; the workflow property `jira.permission.createclone.denied` is set (nothing
to do with screens); or the value is MALFORMED on a field that IS on the screen. A self-hosted
DC instance with a customised Create screen hits this on the first write.

A SECOND DEFECT IN THE SAME BLOCK, found while tracing the first. The write-ahead records the
key BEFORE the identity writes (`record_pending_key` + `save`, deliberately inside the try).
The rollback then deleted the issue but NEVER UNBOUND, so a KEYED-pending binding survived
pointing at a DELETED Jira key. On every later pass `recover_pending_bindings`' keyed branch
calls `add_label` on that dead key, raises, is caught into the failure sink, and the entry
stays pending — permanently. So the old behaviour did not merely lose an issue; it also
stranded the local ticket forever. Both symptoms have one cause and one fix.

WHY NOT-DELETING IS SAFE. The recovery path this needs already exists and is already correct:
the KEYED branch retro-attaches the rebar-id label and the local_id property and confirms, with
NO Jira search — deterministic, and documented as such precisely so a crash in the
create->label window yields no duplicate. A failed identity write leaves exactly the state that
branch is built for. The rollback was solving a problem the write-ahead protocol had already
solved, and destroying data to do it.
"""

from __future__ import annotations

import pytest

from rebar_reconciler.binding_store import BindingStore
from rebar_reconciler.dispatch_one import create_one


@pytest.fixture
def store(tmp_path) -> BindingStore:
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir(parents=True, exist_ok=True)
    return BindingStore(tracker)


class _IdentityWriteFailsClient:
    """`create_issue` succeeds; the identity write fails — the exact defect trigger.

    ``add_label`` raises the misleading screen error Jira actually returns.
    """

    def __init__(self, *, fail_label: bool = True) -> None:
        self.created: list[str] = []
        self.deleted: list[str] = []
        self.labelled: list[tuple] = []
        self.props: list[tuple] = []
        self.fail_label = fail_label

    def search_issues(self, jql, *a, **k):
        return []

    def create_issue(self, fields):
        key = f"DC-{len(self.created) + 1}"
        self.created.append(key)
        return {"key": key}

    def add_label(self, key, label):
        if self.fail_label:
            raise RuntimeError(
                "Field 'labels' cannot be set. It is not on the appropriate screen, or unknown"
            )
        self.labelled.append((key, label))

    def set_entity_property(self, key, name, value):
        self.props.append((key, name, value))

    def delete_issue(self, key):
        self.deleted.append(key)
        return {"deleted": key}


def _mutation() -> dict:
    return {"local_id": "t1", "fields": {"summary": "s", "issuetype": "Task"}}


def test_a_failed_identity_write_does_not_delete_the_created_issue(store, tmp_path) -> None:
    """THE BUG. Deleting a successfully-created issue to recover from a failure to LABEL it
    destroys the only irreplaceable thing in the transaction."""
    client = _IdentityWriteFailsClient()

    with pytest.raises(RuntimeError):
        create_one(_mutation(), client, binding_store=store, repo_root=tmp_path)

    assert client.created == ["DC-1"], "precondition: the issue was created"
    assert client.deleted == [], (
        "the created Jira issue was DELETED because labelling it failed — a "
        "created-but-unlabelled issue is recoverable via retro-attach; a deleted one is not"
    )


def test_the_binding_is_left_keyed_pending_on_the_created_key(store, tmp_path) -> None:
    """The state that makes not-deleting safe. Without the key recorded, the issue would be
    an orphan; with it, the deterministic keyed-recovery branch can finish the job."""
    client = _IdentityWriteFailsClient()
    with pytest.raises(RuntimeError):
        create_one(_mutation(), client, binding_store=store, repo_root=tmp_path)

    assert store.is_pending("t1"), "the binding is not pending, so recovery will never retry it"
    assert store.get_jira_key("t1") == "DC-1", (
        "the pending binding does not carry the created key, so recovery would fall to the "
        "SEARCH branch — which is index-dependent and can duplicate on DC"
    )


def test_recovery_completes_the_identity_write_and_confirms(store, tmp_path) -> None:
    """END-TO-END, and the assertion that proves not-deleting is CORRECT rather than merely
    less destructive: the next pass finishes the job against the SAME issue."""
    client = _IdentityWriteFailsClient()
    with pytest.raises(RuntimeError):
        create_one(_mutation(), client, binding_store=store, repo_root=tmp_path)

    client.fail_label = False  # the screen is fixed / the transient cleared
    resolved = store.recover_pending_bindings(client)

    assert resolved == 1
    assert store.get_jira_key("t1") == "DC-1"
    assert ("DC-1", "rebar-id:t1") in client.labelled
    assert client.created == ["DC-1"], "recovery created a SECOND issue instead of adopting"


def test_no_duplicate_issue_is_created_on_a_later_pass(store, tmp_path) -> None:
    """The failure mode not-deleting must not introduce: a retained issue plus a re-created
    one. Once recovery confirms the binding, the create gate sees it as bound."""
    client = _IdentityWriteFailsClient()
    with pytest.raises(RuntimeError):
        create_one(_mutation(), client, binding_store=store, repo_root=tmp_path)
    client.fail_label = False
    store.recover_pending_bindings(client)

    assert store.get_jira_key("t1") == "DC-1"
    assert len(client.created) == 1, f"a duplicate issue was created: {client.created}"


def test_the_failure_is_still_loud(store, tmp_path) -> None:
    """A retained issue must NOT read as success. The original error still propagates
    unmasked, so the item is reported failed and an operator investigates."""
    client = _IdentityWriteFailsClient()
    with pytest.raises(RuntimeError) as excinfo:
        create_one(_mutation(), client, binding_store=store, repo_root=tmp_path)
    assert "appropriate screen" in str(excinfo.value), (
        f"the original write error was masked: {excinfo.value!r}"
    )


def test_a_bridge_alert_is_still_written(store, tmp_path) -> None:
    """Observability is unchanged: the operator still gets an alert event. Only the
    destructive half is removed."""
    client = _IdentityWriteFailsClient()
    with pytest.raises(RuntimeError):
        create_one(_mutation(), client, binding_store=store, repo_root=tmp_path)

    alerts = list((tmp_path / ".tickets-tracker").rglob("*BRIDGE_ALERT.json"))
    assert alerts, "no BRIDGE_ALERT was written — the retained issue is now invisible"


def test_the_alert_does_not_claim_the_issue_was_deleted(store, tmp_path) -> None:
    """TEETH. The alert's reason said "Jira issue deleted". After this change that is FALSE,
    and a false alert is worse than none: it sends the operator looking for a deletion that
    did not happen, in the one place they go for the truth."""
    import json

    client = _IdentityWriteFailsClient()
    with pytest.raises(RuntimeError):
        create_one(_mutation(), client, binding_store=store, repo_root=tmp_path)

    alerts = list((tmp_path / ".tickets-tracker").rglob("*BRIDGE_ALERT.json"))
    body = json.loads(alerts[0].read_text())
    blob = json.dumps(body).lower()
    assert "deleted" not in blob, (
        f"the alert still claims the issue was deleted, which is no longer true: {body!r}"
    )
    assert "retain" in blob or "pending" in blob, (
        f"the alert does not say the issue was RETAINED for retro-attach: {body!r}"
    )


def test_the_happy_path_is_untouched(store, tmp_path) -> None:
    """A successful identity write still labels, sets the property, and confirms."""
    client = _IdentityWriteFailsClient(fail_label=False)
    create_one(_mutation(), client, binding_store=store, repo_root=tmp_path)

    assert client.created == ["DC-1"]
    assert client.deleted == []
    assert ("DC-1", "rebar-id:t1") in client.labelled
    assert store.get_jira_key("t1") == "DC-1"
