"""A bound parent dropped during create mapping or rejected by Jira's attach call must leave a
durable bridge alert. Unbound, converged, and successful parents stay silent; recording must
not change payloads or control flow."""

from __future__ import annotations

import importlib
import io
import json
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from rebar.reconciler import scratch_retention

REPO_ROOT = Path(__file__).resolve().parents[4]
_REC = REPO_ROOT / "src" / "rebar" / "reconciler"


class _Bindings:
    """A binding store over an explicit local_id -> jira_key map."""

    def __init__(self, bindings: dict[str, str]) -> None:
        self._b = bindings

    def get_jira_key(self, local_id: str) -> str | None:
        return self._b.get(local_id)

    def get_local_id(self, key: str) -> str | None:
        return {v: k for k, v in self._b.items()}.get(key)

    def is_bound(self, local_id: str) -> bool:
        return local_id in self._b

    def get_baseline(self, local_id: str) -> None:
        return None

    def is_pending(self, local_id: str) -> bool:
        return False

    def retired_key_for_local(self, local_id: str) -> None:
        return None


def _tickets(parent_type: str) -> list[dict[str, Any]]:
    """Build an unbound child with a configurable parent type and binding state."""
    return [
        {"ticket_id": "parent-local", "ticket_type": parent_type, "title": "P", "status": "open"},
        {
            "ticket_id": "child-local",
            "ticket_type": "task",
            "title": "C",
            "status": "open",
            "parent_id": "parent-local",
        },
    ]


def _create_mutations(
    parent_type: str,
    *,
    parent_bound: bool = True,
    dropped: list[tuple[str, str]] | None = None,
) -> list[Any]:
    """Drive the REAL differ over an unbound child, returning its outbound mutations.

    Goes through ``compute_outbound_mutations`` (not the mapper directly) because the contract
    under test is that the drop reaches the pass's sink, and only the differ owns that sink.
    """
    from rebar.reconciler import outbound_differ
    from rebar.reconciler.adapters.jira.backend import _JiraOutbound

    bindings = {"parent-local": "RBJ-1"} if parent_bound else {}
    config = outbound_differ.OutboundDiffConfig(
        client=None, pass_id="pass-8390", dropped_field_sink=dropped
    )
    mutations, _ = outbound_differ.compute_outbound_mutations(
        _tickets(parent_type),
        {},
        _Bindings(bindings),
        config=config,
        outbound_mapper=_JiraOutbound(),
    )
    return [m for m in mutations if m.local_id == "child-local"]


def _http_400() -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://example.invalid/rest/api/2/issue/RBJ-9", 400, "Bad Request", {}, io.BytesIO(b"")
    )


class _RejectingClient:
    """A transport whose ``set_parent`` is refused by Jira on hierarchy grounds."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc
        self.calls: list[tuple[str, str]] = []

    def set_parent(self, child_key: str, parent_key: str) -> None:
        self.calls.append((child_key, parent_key))
        raise self._exc


def _alert_kinds(repo_root: Path) -> list[str]:
    """Read durable bridge-alert kinds from the repository, rather than an in-memory sink."""
    kinds: list[str] = []
    for path in sorted(scratch_retention.alerts_dir(repo_root).glob("*.jsonl")):
        for line in path.read_text().splitlines():
            if line.strip():
                kinds.append(json.loads(line).get("kind", ""))
    return kinds


# ---------------------------------------------------------------------------
# SLOT 2 — the CREATE emit
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_a_create_that_drops_a_bound_non_epic_parent_is_recorded() -> None:
    """A bound non-epic parent omitted from create payload must be recorded as dropped."""
    dropped: list[tuple[str, str]] = []
    mutations = _create_mutations("task", dropped=dropped)

    assert len(mutations) == 1 and mutations[0].action == "create", (
        f"precondition: the unbound child must produce exactly one CREATE: {mutations!r}"
    )
    assert "parent" not in mutations[0].fields, (
        f"the 8b25 hierarchy guard must still suppress the WRITE — rebar must not send a parent "
        f"Jira rejects with HTTP 400. fields={mutations[0].fields!r}"
    )
    assert ("child-local", "parent") in dropped, (
        f"a CREATE dropped a BOUND non-epic parent and NOTHING recorded it: dropped={dropped!r}. "
        f"The pass exits 0 reporting a successful create while the ticket is born unparented, and "
        f"the only trace is a logger.debug no operator reads. This is the create-time twin of the "
        f"emit-time silence 9f26 closed and the apply-time silence 39c1 closed."
    )


@pytest.mark.unit
def test_a_create_whose_non_epic_parent_is_UNBOUND_records_nothing() -> None:
    """An unbound parent can resolve on a later pass, so its omission must not alert."""
    dropped: list[tuple[str, str]] = []
    mutations = _create_mutations("task", parent_bound=False, dropped=dropped)

    assert "parent" not in mutations[0].fields, (
        f"an unbound parent is still omitted from the payload: {mutations[0].fields!r}"
    )
    assert dropped == [], (
        f"an UNBOUND parent was reported as a dropped field: dropped={dropped!r}. It converges on "
        f"a later pass once the binding exists, so this alert fires on ordinary create ordering "
        f"and drains the channel of meaning."
    )


@pytest.mark.unit
def test_a_create_with_an_EPIC_parent_emits_the_parent_and_records_nothing() -> None:
    """An accepted epic parent must be emitted without a drop alert."""
    dropped: list[tuple[str, str]] = []
    mutations = _create_mutations("epic", dropped=dropped)

    assert mutations[0].fields.get("parent") == "RBJ-1", (
        f"an epic parent must still be emitted on the create payload: {mutations[0].fields!r}"
    )
    assert dropped == [], f"nothing was dropped, so nothing may be recorded: dropped={dropped!r}"


@pytest.mark.unit
def test_the_create_path_still_works_with_NO_sink_supplied() -> None:
    """The optional alert sink must not affect create payloads or raise when absent."""
    mutations = _create_mutations("task", dropped=None)

    assert len(mutations) == 1 and "parent" not in mutations[0].fields, (
        f"suppression and payload must be unchanged without a sink: {mutations!r}"
    )


@pytest.mark.unit
def test_the_dropped_create_parent_reaches_the_bridge_alert_store(tmp_path: Path) -> None:
    """The sink is a means, not the end — drive the REAL emitter to the REAL alert store.

    Without this the cells above could pass while the pair went nowhere: ``dropped_field_sink`` is
    only a signal if ``_emit_outbound_field_alerts`` files it.
    """
    run_differs = importlib.import_module("rebar.reconciler.run_differs")
    alert_store = importlib.import_module("rebar.reconciler.alert_store")

    dropped: list[tuple[str, str]] = []
    _create_mutations("task", dropped=dropped)
    assert ("child-local", "parent") in dropped, "precondition: the drop must be recorded first"

    run_differs._emit_outbound_field_alerts([], dropped, tmp_path, "pass-8390")

    assert alert_store.is_deduped(
        "outbound-field-dropped:child-local:parent", repo_root=tmp_path
    ), (
        "the dropped create parent was recorded in the sink but never became a durable "
        "`outbound-field-dropped` bridge alert, so it is still invisible to an operator — the "
        "sink alone is not the contract."
    )


# ---------------------------------------------------------------------------
# SLOT 4 — the ACLI create-then-attach fallback
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_an_ACLI_attach_rejected_with_400_records_a_parent_divergence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Jira 400 parent rejection must reach the durable outbound-parent-rejected alert."""
    monkeypatch.setenv("REBAR_ROOT", str(tmp_path))
    acli_cli_ops = importlib.import_module("rebar.reconciler.adapters.jira.acli_cli_ops")
    client = _RejectingClient(_http_400())

    acli_cli_ops._attach_parent_guarded(client, "RBJ-9", "RBJ-1")

    assert client.calls == [("RBJ-9", "RBJ-1")], (
        f"precondition: the attach must actually have been attempted: {client.calls!r}"
    )
    assert "outbound-parent-rejected" in _alert_kinds(tmp_path), (
        f"Jira REJECTED the parent with HTTP 400 and rebar recorded nothing durable — the create "
        f"is reported as successful and the hierarchy the user asked for is silently gone. "
        f"kinds={_alert_kinds(tmp_path)!r}. `dispatch_one` records exactly this kind for exactly "
        f"this failure; the create-then-attach fallback is the same event through another door."
    )


@pytest.mark.unit
def test_the_ACLI_400_is_still_SWALLOWED_so_the_create_still_lands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A parent-only Jira 400 stays non-fatal even when its alert is recorded."""
    monkeypatch.setenv("REBAR_ROOT", str(tmp_path))
    acli_cli_ops = importlib.import_module("rebar.reconciler.adapters.jira.acli_cli_ops")

    assert (
        acli_cli_ops._attach_parent_guarded(_RejectingClient(_http_400()), "RBJ-9", "RBJ-1") is None
    ), "the 400 arm must keep returning None (warn-and-continue), not raise"


@pytest.mark.unit
def test_a_NON_400_attach_error_still_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard is scoped to hierarchy rejections; a 500 is a real failure and must not be eaten.

    Pins that adding the recording did not widen the except clause into a blanket swallow.
    """
    monkeypatch.setenv("REBAR_ROOT", str(tmp_path))
    acli_cli_ops = importlib.import_module("rebar.reconciler.adapters.jira.acli_cli_ops")
    boom = urllib.error.HTTPError(
        "https://example.invalid", 500, "Server Error", {}, io.BytesIO(b"")
    )

    with pytest.raises(urllib.error.HTTPError):
        acli_cli_ops._attach_parent_guarded(_RejectingClient(boom), "RBJ-9", "RBJ-1")


@pytest.mark.unit
def test_a_SUCCESSFUL_attach_records_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The negative control for slot 4: the parent reached the tracker, so there is no divergence.

    Without it, a fix that recorded on every attach would pass the headline cell while making
    ``outbound-parent-rejected`` mean "a parent was attached".
    """
    monkeypatch.setenv("REBAR_ROOT", str(tmp_path))
    acli_cli_ops = importlib.import_module("rebar.reconciler.adapters.jira.acli_cli_ops")

    class _OK:
        def set_parent(self, child_key: str, parent_key: str) -> None:
            return None

    acli_cli_ops._attach_parent_guarded(_OK(), "RBJ-9", "RBJ-1")

    assert _alert_kinds(tmp_path) == [], (
        f"the attach SUCCEEDED — nothing diverged, so nothing may be recorded: "
        f"{_alert_kinds(tmp_path)!r}"
    )


# ---------------------------------------------------------------------------
# THE UPDATE EMIT MUST KEEP ITS CONVERGED-VS-DIVERGENT DISCRIMINATION
# ---------------------------------------------------------------------------


class _PassthroughOutboundMapper:
    def map_fields_to_remote(self, changed: dict[str, Any], **_kw: Any) -> dict[str, Any]:
        return dict(changed)

    def resolve_assignee(
        self, local_value: Any, _remote_identity: Any, *, assignee_resolver: Any = None
    ) -> tuple[Any, bool, bool]:
        return (local_value, False, False)


def _update_drop(remote_parent_id: str | None) -> list[tuple[str, str]]:
    """Vary the remote parent to distinguish a dropped update from an already converged one."""
    from rebar.reconciler.outbound_field_diff import diff_canonical_fields

    dropped: list[tuple[str, str]] = []
    diff_canonical_fields(
        {
            "ticket_id": "child-local",
            "title": "T",
            "description": "D",
            "ticket_type": "task",
            "priority": 2,
            "status": "open",
            "assignee": "",
            "parent_id": "parent-local",
        },
        {
            "title": "T",
            "description": "D",
            "priority": 2,
            "status": "open",
            "assignee": "",
            "remote_parent_id": remote_parent_id,
        },
        None,
        outbound_mapper=_PassthroughOutboundMapper(),
        binding_store=_Bindings({"parent-local": "RBJ-1", "child-local": "RBJ-9"}),
        local_ticket_types={"parent-local": "task"},
        jira_key="RBJ-9",
        local_id="child-local",
        dropped_field_sink=dropped,
    )
    return dropped


@pytest.mark.unit
@pytest.mark.parametrize(
    ("remote_parent_id", "expected"),
    [
        (None, [("RBJ-9", "parent")]),  # the tracker holds NO parent — the edit was lost
        ("RBJ-1", []),  # the tracker ALREADY holds it — nothing was dropped
        ("RBJ-7", [("RBJ-9", "parent")]),  # the tracker holds a DIFFERENT parent — still lost
    ],
)
def test_the_update_emit_still_discriminates_converged_from_dropped(
    remote_parent_id: str | None, expected: list[tuple[str, str]]
) -> None:
    """Update alerts only for a divergent parent; an already converged parent stays silent."""
    assert _update_drop(remote_parent_id) == expected, (
        f"UPDATE emit with remote_parent_id={remote_parent_id!r} must record {expected!r}; a "
        f"tracker that already carries the parent dropped nothing, and alerting there would fire "
        f"every pass for every inbound-mirrored sub-task and invert the alert's meaning."
    )
