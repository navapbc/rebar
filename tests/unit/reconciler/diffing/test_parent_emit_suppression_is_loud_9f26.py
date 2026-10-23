"""Suppressing a bound non-epic parent write must leave a durable drop alert only when the
remote parent differs. Unbound and converged cases stay silent; the parent remains out of the
outbound payload."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest

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


class _PassthroughOutboundMapper:
    """The vendor port, neutralized — this file is about the parent gate, not mapping."""

    def map_fields_to_remote(self, changed: dict[str, Any], **_kw: Any) -> dict[str, Any]:
        return dict(changed)

    def resolve_assignee(
        self, local_value: Any, _remote_identity: Any, *, assignee_resolver: Any = None
    ) -> tuple[Any, bool, bool]:
        return (local_value, False, False)


def _ticket(parent_id: str | None = None) -> dict[str, Any]:
    """A local ticket whose ONLY divergence from the remote below is its parent."""
    t: dict[str, Any] = {
        "ticket_id": "child-local",
        "title": "T",
        "description": "D",
        "ticket_type": "task",
        "priority": 2,
        "status": "open",
        "assignee": "",
    }
    if parent_id is not None:
        t["parent_id"] = parent_id
    return t


def _remote(remote_parent_id: str | None = None) -> dict[str, Any]:
    """Vary the remote parent while keeping every other field converged."""
    return {
        "title": "T",
        "description": "D",
        "priority": 2,
        "status": "open",
        "assignee": "",
        "remote_parent_id": remote_parent_id,
    }


def _diff(
    ticket: dict[str, Any],
    bindings: dict[str, str],
    types: dict[str, str] | None,
    dropped: list[tuple[str, str]] | None,
    *,
    remote_parent_id: str | None = None,
) -> dict[str, Any]:
    from rebar.reconciler.outbound_field_diff import diff_canonical_fields

    return diff_canonical_fields(
        ticket,
        _remote(remote_parent_id),
        None,
        outbound_mapper=_PassthroughOutboundMapper(),
        binding_store=_Bindings(bindings),
        local_ticket_types=types,
        jira_key="RBJ-9",
        local_id="child-local",
        dropped_field_sink=dropped,
    )


# ---------------------------------------------------------------------------
# THE DEFECT: a suppressed non-epic parent must leave a durable record
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_a_suppressed_non_epic_parent_is_recorded_as_a_dropped_field() -> None:
    """A divergent bound non-epic parent stays out of changed and enters the drop sink."""
    dropped: list[tuple[str, str]] = []
    changed = _diff(
        _ticket("parent-local"),
        {"parent-local": "RBJ-1", "child-local": "RBJ-9"},
        {"parent-local": "task"},
        dropped,
    )

    assert "parent" not in changed, (
        f"the 8b25 hierarchy guard must still suppress the WRITE — rebar must not send a parent "
        f"the tracker rejects or silently ignores. changed={changed!r}"
    )
    assert ("RBJ-9", "parent") in dropped, (
        f"a non-epic parent was suppressed and NOTHING recorded it: dropped={dropped!r}. The "
        f"pass will exit 0 and report convergence while the user's parent edit is gone. This is "
        f"the emit-time twin of the apply-time divergence 39c1 made durable, and it is the same "
        f"invisibility that let this class of bug run to five instances."
    )


@pytest.mark.unit
def test_an_ALREADY_CONVERGED_non_epic_parent_is_NOT_recorded() -> None:
    """A matching Jira parent is converged, so suppression must not create an alert."""
    dropped: list[tuple[str, str]] = []
    changed = _diff(
        _ticket("parent-local"),
        {"parent-local": "RBJ-1", "child-local": "RBJ-9"},
        {"parent-local": "task"},
        dropped,
        remote_parent_id="RBJ-1",  # the tracker ALREADY holds exactly this parent
    )

    assert "parent" not in changed, (
        f"a converged parent must not be emitted — there is nothing to write: {changed!r}"
    )
    assert dropped == [], (
        f"an ALREADY-CONVERGED non-epic parent was reported as a dropped field: "
        f"dropped={dropped!r}. Nothing was dropped: the tracker holds 'RBJ-1' and so does the "
        f"local ticket. Every inbound-mirrored sub-task in the store looks like this, so this "
        f"fires on every pass for every one of them and the alert stops meaning 'a hierarchy "
        f"edit was lost'."
    )


@pytest.mark.unit
def test_a_DIVERGENT_non_epic_parent_IS_recorded_even_when_remote_has_another_parent() -> None:
    """A different remote parent is a real dropped edit and must alert."""
    dropped: list[tuple[str, str]] = []
    changed = _diff(
        _ticket("parent-local"),
        {"parent-local": "RBJ-1", "child-local": "RBJ-9"},
        {"parent-local": "task"},
        dropped,
        remote_parent_id="RBJ-7",  # the tracker holds a DIFFERENT parent
    )

    assert "parent" not in changed, f"the guard must still suppress the write: {changed!r}"
    assert ("RBJ-9", "parent") in dropped, (
        f"the user asked for parent 'RBJ-1', the tracker holds 'RBJ-7', and the guard dropped "
        f"the change with NOTHING recorded: dropped={dropped!r}. That is a lost hierarchy edit."
    )


@pytest.mark.unit
def test_an_unbound_NON_EPIC_parent_is_NOT_recorded_because_it_may_still_converge() -> None:
    """An unbound non-epic parent may resolve next pass and must not alert."""
    dropped: list[tuple[str, str]] = []
    changed = _diff(
        _ticket("parent-local"),
        {"child-local": "RBJ-9"},  # the PARENT is deliberately unbound
        {"parent-local": "task"},  # and NON-epic, so the suppression branch IS entered
        dropped,
    )

    assert "parent" not in changed, f"an unbound parent has no key to emit yet: {changed!r}"
    assert dropped == [], (
        f"an UNBOUND non-epic parent was reported as a dropped field: dropped={dropped!r}. The "
        f"guard fired, but nothing has been dropped that the tracker was ever offered — this "
        f"alerts every pass on a condition that is still resolving. If this fires, the headline "
        f"assertion above proves nothing, because 'the guard fired' and 'a hierarchy edit was "
        f"permanently lost' would again be indistinguishable."
    )


@pytest.mark.unit
def test_an_unbound_epic_parent_is_NOT_recorded() -> None:
    """The same silence for the shape that never enters the guarded branch at all, so a future
    refactor that moves the recording ABOVE the epic check is caught rather than absorbed."""
    dropped: list[tuple[str, str]] = []
    changed = _diff(
        _ticket("parent-local"),
        {"child-local": "RBJ-9"},  # unbound parent
        {"parent-local": "epic"},
        dropped,
    )

    assert "parent" not in changed, f"an unbound parent has no key to emit yet: {changed!r}"
    assert dropped == [], (
        f"an UNBOUND EPIC parent was reported as a dropped field: dropped={dropped!r}. Nothing "
        f"was suppressed here — the parent simply has no key yet and will land next pass."
    )


@pytest.mark.unit
def test_an_epic_parent_is_emitted_and_not_recorded() -> None:
    """An accepted epic parent is emitted without a drop alert."""
    dropped: list[tuple[str, str]] = []
    changed = _diff(
        _ticket("parent-local"),
        {"parent-local": "RBJ-1", "child-local": "RBJ-9"},
        {"parent-local": "epic"},
        dropped,
    )

    assert changed.get("parent") == "RBJ-1", (
        f"an EPIC parent must still be emitted — that is the one shape both the emit gate and the "
        f"DC apply side accept (live-green via the Epic Link). changed={changed!r}"
    )
    assert dropped == [], f"a parent that WAS emitted must not be reported as dropped: {dropped!r}"


@pytest.mark.unit
def test_a_type_unknown_parent_is_emitted_and_not_recorded() -> None:
    """A parent absent from the type map falls through to emit (the pre-8b25 behaviour it
    deliberately preserves), so it is not a drop either. Pins that the fix reads the guard's
    actual decision rather than re-deriving it and diverging."""
    dropped: list[tuple[str, str]] = []
    changed = _diff(
        _ticket("parent-local"),
        {"parent-local": "RBJ-1", "child-local": "RBJ-9"},
        {},  # type map supplied but does not carry the parent
        dropped,
    )

    assert changed.get("parent") == "RBJ-1", (
        f"a parent of UNKNOWN type must fall through to emit — the guard declines to guess and "
        f"the applier-side 400-skip is the backstop. changed={changed!r}"
    )
    assert dropped == [], f"a parent that WAS emitted must not be reported as dropped: {dropped!r}"


@pytest.mark.unit
def test_no_sink_supplied_does_not_break_the_diff() -> None:
    """Observability must never cost delivery: the sink is optional everywhere else it is used,
    and a caller that passes none must still get a correct diff rather than an AttributeError."""
    changed = _diff(
        _ticket("parent-local"),
        {"parent-local": "RBJ-1", "child-local": "RBJ-9"},
        {"parent-local": "task"},
        None,
    )
    assert "parent" not in changed, f"suppression must be unchanged without a sink: {changed!r}"


# ---------------------------------------------------------------------------
# END TO END: the recorded pair must become a real bridge alert
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_suppressed_parent_reaches_the_bridge_alert_store(tmp_path: Path) -> None:
    """The real emitter must persist the suppressed-parent alert beyond the in-memory sink."""
    run_differs = importlib.import_module("rebar.reconciler.run_differs")
    alert_store = importlib.import_module("rebar.reconciler.alert_store")

    dropped: list[tuple[str, str]] = []
    _diff(
        _ticket("parent-local"),
        {"parent-local": "RBJ-1", "child-local": "RBJ-9"},
        {"parent-local": "task"},
        dropped,
    )
    assert ("RBJ-9", "parent") in dropped, "precondition: the drop must be recorded first"

    run_differs._emit_outbound_field_alerts([], dropped, tmp_path, "pass-9f26")

    assert alert_store.is_deduped("outbound-field-dropped:RBJ-9:parent", repo_root=tmp_path), (
        "the suppressed parent was recorded in the sink but never became a durable "
        "`outbound-field-dropped` bridge alert, so it is still invisible to an operator — the "
        "sink alone is not the contract."
    )
