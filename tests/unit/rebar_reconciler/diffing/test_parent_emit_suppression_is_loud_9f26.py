"""Bug 9f26: an emit-time parent suppression must be OPERATOR-VISIBLE, not a bare return.

THE DEFECT. ``outbound_field_diff._resolve_local_parent`` returns ``(False, None)`` for a parent
whose local ``ticket_type`` is not ``epic`` — bug 8b25's hierarchy guard, which is correct and
stays. What is wrong is that the return is **byte-identical to the "unbound this pass, retry next
pass" return a few lines below it**, and carries no log call of any kind. The caller does nothing
when ``present`` is false. So a user's parent edit against a non-epic parent never enters
``fields``, ``dispatch_one._update_one_apply_parent`` never runs, and the whole
``record_parent_divergence`` / ``bridge_alerts`` apparatus is structurally unreachable: the pass
exits 0, reports convergence, and the user's hierarchy change is gone with nothing anywhere saying
so.

WHY THAT IS A DEFECT AND NOT A DESIGN CHOICE, cited. ``pass_io.py``'s own contract for this exact
shape of failure: "a log line is not a signal: the pass still exits 0 and *nothing durable says the
hierarchy diverged*". And ``dispatch_one.py``: "Warn-and-continue alone is why 'the parent never
reached the tracker' stayed invisible through FIVE instances of this class". Ticket 39c1 wired that
contract into *apply-time* parent failure and left *emit-time* suppression silent — the same
operator-visible outcome through a different door. This file closes that door.

THE TWO STATES MUST STAY DISTINGUISHABLE, which is why the negative control below is load-bearing
rather than decoration. A suppressed non-epic parent is a DIVERGENCE (it will never converge — the
guard suppresses it on every pass, forever). An unbound parent is NOT (it converges next pass, once
the binding exists). Alerting on both would train operators to ignore the alert, so the fix is only
correct if it fires for the first and stays silent for the second. Before the fix, no signal
distinguishes them because both are ``(False, None)``.

THE CHANNEL IS THE EXISTING ONE. ``dropped_field_sink`` already collects ``(jira_key, field)`` for a
mapped-but-not-emitted field, and ``run_differs._emit_outbound_field_alerts`` already turns it into
a deduped ``outbound-field-dropped`` bridge alert — the acd0 precedent, pinned for ``issuetype`` by
``test_outbound_field_conflict_and_drop_alerts.py``. A suppressed parent is the same shape of event,
so it takes the same channel rather than inventing plumbing. The last test drives the REAL emitter
so this is not merely an assertion about a list.

THE GUARD IS NOT LIFTED. Every test here also asserts ``parent`` is still absent from ``changed``.
rebar must keep declining to write a parent the tracker would reject or silently ignore; the fix
adds the missing SIGNAL, it does not add a write.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
_REC = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler"


def _load(name: str, path: Path) -> ModuleType:
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


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

    def resolve_assignee(self, local_value: Any, _remote_identity: Any) -> tuple[Any, bool, bool]:
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
    """The canonical remote snapshot: converged on every field except as parameterized.

    ``remote_parent_id`` is a PARAMETER and not a hardcoded ``None``, and that is not a
    convenience. A fixture that pins it to ``None`` can only ever describe a tracker with no
    parent, so it cannot distinguish "the parent was dropped" from "the parent is already
    there" — and the second case is the common one, since an inbound-mirrored child arrives
    carrying whatever parent the tracker holds. Pinning it is how the first version of this
    file passed while the code alerted on every converged ticket.
    """
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
    from rebar_reconciler.outbound_field_diff import diff_canonical_fields

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
    """The headline: the divergence the guard creates must be REPORTED, not merely created.

    The parent stays out of ``changed`` (8b25 is preserved) AND the drop is recorded, so an
    operator can see that the hierarchy they asked for is not the hierarchy the tracker holds.
    Asserted on the sink rather than on a log because a log line is explicitly not the contract
    (``pass_io.py``: "a log line is not a signal").
    """
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
    """THE CASE THAT MAKES THE ALERT MEAN SOMETHING — nothing was dropped, so say nothing.

    This is the common shape, not an exotic one. A Jira SUB-TASK's parent genuinely is a
    non-epic (a Story or Task), and the inbound pass mirrors it into local ``parent_id``. So
    every inbound-mirrored sub-task in the store sits here: local parent is non-epic, the
    guard fires, AND the tracker already holds exactly that parent. Nothing is being dropped
    — there is nothing to drop — and an alert here would fire on every pass for every such
    ticket, re-filed each time the 24h dedup window rolls.

    That would invert the alert's meaning: ``outbound-field-dropped:...:parent`` would most
    often mean "nothing was dropped", which is worse than the silence this file set out to
    fix, because a channel that cries wolf is one operators learn to close. It is also the
    rule the sibling ``issuetype`` drop already follows — that sink records only when the
    local value ACTUALLY DIFFERS from the remote one.

    The guard's suppression is unchanged either way: ``parent`` stays out of ``changed``. What
    is under test is purely whether a NON-divergence is reported as a divergence.
    """
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
    """The other side of the same coin: a real divergence must still be reported.

    Guards the over-correction from the test above — a fix that suppresses the alert by
    comparing against the remote must not go silent when the remote holds a DIFFERENT parent,
    which is a genuine lost edit and the whole point of the channel.
    """
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
    """THE LOAD-BEARING NEGATIVE CONTROL — the case that runs INSIDE the guarded branch.

    This is the one that gives the fix teeth, and it is deliberately separate from the epic case
    below. A non-epic parent that is ALSO unbound reaches the very same suppression branch as the
    headline test, so it is the only input that can distinguish "record when the parent is bound"
    from "record whenever the guard fires". A fix that appends unconditionally inside the branch
    passes every other test in this file and fails only here.

    It must stay silent because boundness is what makes the divergence PERMANENT: an unbound
    parent has not yet been offered to the tracker at all, so reporting a dropped hierarchy edge
    for it would file an alert per pass for a condition that is still resolving. A fix that
    cannot tell these two apart has not fixed anything — it has moved the silence into noise.
    """
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
    """The accepted shape must be untouched in BOTH respects: still emitted, and not flagged.

    Guards the opposite over-correction from the negative control above — a fix that records the
    drop by flagging every parent would fire on the one shape that works.
    """
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
    """The sink is a means, not the end — drive the REAL emitter to the REAL alert store.

    Without this, the tests above could pass while the pair went nowhere: ``dropped_field_sink``
    is only a signal if ``_emit_outbound_field_alerts`` files it. Mirrors the acd0 ``issuetype``
    precedent, which pins exactly this hop for the sibling case.
    """
    run_differs = _load("run_differs_parent_drop_9f26", _REC / "run_differs.py")
    alert_store = _load("alert_store_parent_drop_9f26", _REC / "alert_store.py")

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
