"""Bug 8390: the two remaining outbound-parent surfaces that drop a hierarchy edit in silence.

THE RULE, stated as operation + property and deliberately WITHOUT naming any site: **an outbound
parent that does not reach the tracker must leave a durable, operator-visible record — a
``bridge_alerts`` entry — not merely a log line.** That is the project's own contract, cited in the
tree rather than asserted here: ``pass_io.py`` ("a log line is not a signal: the pass still exits 0
and nothing durable says the hierarchy diverged") and ``dispatch_one.py`` ("Warn-and-continue alone
is why 'the parent never reached the tracker' stayed invisible through five instances of this
class").

THE CENSUS. Four surfaces can drop an outbound parent. Ticket 39c1 wired the rule into the UPDATE
apply path; ticket 9f26 wired it into the UPDATE emit path. Two were filed rather than fixed because
they reach into the Cloud/ACLI adapters, a separate blast radius:

* **CREATE emit** — ``adapters/jira/outbound_fields._map_local_to_jira_fields``. The same
  unconditional 8b25 non-epic guard as the UPDATE emit, but its only signal is a ``logger.debug``
  that no operator reads and no test asserted. A ticket CREATED with a non-epic parent loses its
  hierarchy AT BIRTH, which is harder to notice than losing it on an edit.
* **ACLI create-then-attach fallback** — ``adapters/jira/acli_cli_ops._attach_parent_guarded``. It
  swallows an HTTP 400 with a WARNING and returns. This is the only path where Jira ACTIVELY
  REJECTED the parent and rebar still reports the create as successful.

Both silences were measured at runtime before this file was written, each against a positive control
proving the observation channel worked: the 400 attach produced ZERO files under ``REBAR_ROOT``
while a direct ``record_parent_divergence`` under the same root wrote
``bridge_state/bridge_alerts/<date>.jsonl``; and the CREATE path emitted a mutation with no
``parent`` key and an empty sink, while the same call with an ``epic`` parent emitted
``parent="RBJ-1"``.

THE BOUND/UNBOUND DISTINCTION 9f26 ESTABLISHED IS PRESERVED, and the negative controls here are
load-bearing rather than decoration. A BOUND non-epic parent will never converge — the guard
suppresses it on every pass, forever — so it is a real divergence. An UNBOUND parent converges on a
later pass once the binding exists, so alerting on it is churn. A channel that cries wolf is one
operators learn to close, which is worse than the silence this file set out to fix.

THE CONVERGED-VS-DIVERGENT AXIS IS VARIED, NOT PINNED. The UPDATE emit path must keep its
already-converged silence: a tracker that ALREADY holds the parent dropped nothing. That axis is
exercised here in every state — a sibling change once reached two green gates while alerting on
every converged ticket precisely because its fixture hardcoded ``remote_parent_id: None`` and so
could not tell the two apart. These cells exist so a CREATE-side fix cannot leak a spurious drop
onto the UPDATE side.

THE GUARDS ARE NOT LIFTED, on either surface. Every cell also asserts that rebar still declines to
write the parent (no ``parent`` key on the create payload) and that the ACLI 400 is still swallowed
rather than aborting the create. The fix adds the missing SIGNAL; it adds no write, changes no
outbound payload, and changes no return value.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import urllib.error
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

    def retired_key_for_local(self, local_id: str) -> None:
        return None


def _tickets(parent_type: str) -> list[dict[str, Any]]:
    """A parent and an UNBOUND child pointing at it — the CREATE shape.

    ``parent_type`` is a PARAMETER, not a constant: it separates the 8b25 suppression from an
    ordinary epic parent. Boundness is varied by the caller's ``parent_bound``, which separates a
    divergence that will never converge from one that converges on the next pass.
    """
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
    from rebar_reconciler import outbound_differ
    from rebar_reconciler.adapters.jira.backend import _JiraOutbound

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
    """Every ``kind`` written to the bridge-alert store under *repo_root*.

    Reads the durable artifact rather than a spy, because "durable" is the contract: a fix that
    logged louder, or that recorded into an in-memory list, would still leave the operator with
    nothing after the pass exits.
    """
    kinds: list[str] = []
    for path in sorted((repo_root / "bridge_state" / "bridge_alerts").glob("*.jsonl")):
        for line in path.read_text().splitlines():
            if line.strip():
                kinds.append(json.loads(line).get("kind", ""))
    return kinds


# ---------------------------------------------------------------------------
# SLOT 2 — the CREATE emit
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_a_create_that_drops_a_bound_non_epic_parent_is_recorded() -> None:
    """The headline for slot 2: a hierarchy lost AT BIRTH must be reported, not merely created.

    The parent stays off the create payload (8b25 is preserved — rebar must not send a parent the
    tracker rejects) AND the drop is recorded on the existing ``dropped_field_sink``. Asserted on
    the sink and not on a log because a log line is explicitly not the contract.

    The identifier is the LOCAL id: a create has no Jira key yet by construction, which is exactly
    why this surface could not simply reuse the UPDATE path's recording.
    """
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
    """THE NEGATIVE CONTROL THAT KEEPS THE CHANNEL HONEST — an unbound parent converges later.

    A parent with no binding has not been offered to the tracker at all yet; the next pass creates
    it, binds it, and the child's parent resolves. Reporting that as a dropped field would fire for
    every ticket created ahead of its parent — a routine ordering, not an error — and would train
    operators to ignore the channel. This is the same bound/unbound distinction 9f26 established on
    the UPDATE emit; the CREATE emit must not invert it.
    """
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
    """The positive control: nothing was dropped, because the parent is actually being sent.

    Without this cell a fix that recorded a drop unconditionally — or one that stopped emitting the
    parent entirely — would still pass every other cell in this file.
    """
    dropped: list[tuple[str, str]] = []
    mutations = _create_mutations("epic", dropped=dropped)

    assert mutations[0].fields.get("parent") == "RBJ-1", (
        f"an epic parent must still be emitted on the create payload: {mutations[0].fields!r}"
    )
    assert dropped == [], f"nothing was dropped, so nothing may be recorded: dropped={dropped!r}"


@pytest.mark.unit
def test_the_create_path_still_works_with_NO_sink_supplied() -> None:
    """Observability is additive: a caller that supplies no sink gets today's behaviour exactly.

    Pins that the recording is not on the critical path — a differ invoked without the sink (every
    fixture path, and any caller predating this change) must neither raise nor alter the payload.
    """
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
    run_differs = _load("run_differs_parent_drop_8390", _REC / "run_differs.py")
    alert_store = _load("alert_store_parent_drop_8390", _REC / "alert_store.py")

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
    """The headline for slot 4: the ONE path where Jira actively REFUSED the parent.

    Everywhere else the parent is dropped by rebar's own guard before a request is made. Here the
    request went out, Jira answered 400, and rebar returned the create as a success. Swallowing the
    400 is correct — a parent failure must not abort the create — but swallowing it in silence is
    the exact shape ``dispatch_one`` already records via ``record_parent_divergence``, under the
    ``outbound-parent-rejected`` kind that means "Jira refused THIS parent on hierarchy grounds".

    Asserted against the durable store on disk, not a spy, because durability is the contract.
    """
    monkeypatch.setenv("REBAR_ROOT", str(tmp_path))
    acli_cli_ops = _load("acli_cli_ops_parity_8390", _REC / "adapters" / "jira" / "acli_cli_ops.py")
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
    """Recording must not change control flow: the 400 stays non-fatal.

    Observability can never cost delivery. A fix that raised, or that let the alert store's own
    failure escape, would turn an issue that was successfully created into a failed pass.
    """
    monkeypatch.setenv("REBAR_ROOT", str(tmp_path))
    acli_cli_ops = _load("acli_cli_ops_parity_8390", _REC / "adapters" / "jira" / "acli_cli_ops.py")

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
    acli_cli_ops = _load("acli_cli_ops_parity_8390", _REC / "adapters" / "jira" / "acli_cli_ops.py")
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
    acli_cli_ops = _load("acli_cli_ops_parity_8390", _REC / "adapters" / "jira" / "acli_cli_ops.py")

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

    def resolve_assignee(self, local_value: Any, _remote_identity: Any) -> tuple[Any, bool, bool]:
        return (local_value, False, False)


def _update_drop(remote_parent_id: str | None) -> list[tuple[str, str]]:
    """Run the UPDATE emit diff for a bound child whose non-epic parent is suppressed.

    ``remote_parent_id`` is a PARAMETER and not a hardcoded ``None``. A fixture that pins it to
    ``None`` can only describe a tracker holding no parent, so it cannot tell "the parent was
    dropped" from "the parent is already there" — and the second is the common shape, since a Jira
    sub-task's parent genuinely IS a non-epic that inbound mirrors into local ``parent_id``.
    Hardcoding it is precisely how a sibling change shipped past two green gates while alerting on
    every converged ticket.
    """
    from rebar_reconciler.outbound_field_diff import diff_canonical_fields

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
    "remote_parent_id,expected",
    [
        (None, [("RBJ-9", "parent")]),  # the tracker holds NO parent — the edit was lost
        ("RBJ-1", []),  # the tracker ALREADY holds it — nothing was dropped
        ("RBJ-7", [("RBJ-9", "parent")]),  # the tracker holds a DIFFERENT parent — still lost
    ],
)
def test_the_update_emit_still_discriminates_converged_from_dropped(
    remote_parent_id: str | None, expected: list[tuple[str, str]]
) -> None:
    """The axis a CREATE-side fix must not leak onto: 9f26's convergence gate stays intact.

    The CREATE path has no convergence question — the issue does not exist yet, so a dropped parent
    is unconditionally a real loss. The UPDATE path does, and its answer must not change: an
    already-converged parent stays silent. Varying the same fixture across all three tracker states
    is what makes that a measurement rather than an assumption.
    """
    assert _update_drop(remote_parent_id) == expected, (
        f"UPDATE emit with remote_parent_id={remote_parent_id!r} must record {expected!r}; a "
        f"tracker that already carries the parent dropped nothing, and alerting there would fire "
        f"every pass for every inbound-mirrored sub-task and invert the alert's meaning."
    )
