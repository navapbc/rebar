"""Per-leaf BEHAVIORAL EFFECT MATRIX for the ``_LEAVES`` dispatch registry.

This replaces the old AST/regex "real body" heuristic (which passed while a leaf
sent the WRONG target, DROPPED a field, returned the WRONG follow-on, or SWALLOWED
an error) with a matrix that drives every ``(direction, action)`` leaf against a
mock Jira client / isolated tracker and asserts the *observable effect*:

  * the EXACT client/store method invoked and the EXACT target entity id it was
    called with (a wrong-target defect fails);
  * the EXACT payload field set sent (a dropped/renamed field fails);
  * the returned ``ApplyResult`` and any follow-on (a wrong follow-on fails);
  * for every leaf with an error/exception path, that path (raise / failure
    ``ApplyResult``); for a leaf with no error path, its success ``ApplyResult``.

Two guarantees:
  1. ``test_leaf_behavioral_effect`` — parametrised over the matrix, drives each
     leaf and asserts the above.
  2. ``test_matrix_covers_every_leaf`` — COMPLETENESS: the matrix's key set is
     derived-and-compared against ``_LEAVES``, so a newly-added leaf with no
     behavioural coverage FAILS this test.

The leaves are driven straight out of ``applier._LEAVES`` (the registry under
test) — the matrix key IS the ``(direction, action)`` pair, and the handler is
looked up from the live table, so the coverage claim is structural.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from rebar_reconciler import apply_outbound
from rebar_reconciler._errors import JiraAPIError

REPO_ROOT = Path(__file__).resolve().parents[4]
APPLIER_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "applier.py"
MUTATION_PATH = APPLIER_PATH.parent / "mutation.py"


def _load_mutation_module():
    # Load under the canonical key so Mutation objects share class identity with
    # the enum members the leaves see (avoids _direction_guard false negatives).
    canonical = "rebar_reconciler.mutation"
    if canonical in sys.modules:
        return sys.modules[canonical]
    spec = importlib.util.spec_from_file_location(canonical, MUTATION_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[canonical] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _load_applier():
    spec = importlib.util.spec_from_file_location("applier_effect_matrix", APPLIER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["applier_effect_matrix"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def mut_mod():
    return _load_mutation_module()


@pytest.fixture(scope="module")
def applier():
    return _load_applier()


@pytest.fixture
def fixture_repo(tmp_path, monkeypatch):
    """A minimal repo with an initialised ``.tickets-tracker`` dir.

    Strips REBAR_TRACKER_DIR / REBAR_ENV_ID / REBAR_AUTHOR so a developer's
    shell env cannot steer writes away from the tmp tracker or diverge event
    metadata between local and CI.
    """
    monkeypatch.delenv("REBAR_TRACKER_DIR", raising=False)
    monkeypatch.delenv("REBAR_ENV_ID", raising=False)
    monkeypatch.delenv("REBAR_AUTHOR", raising=False)
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    (tracker / ".env-id").write_text("test-env-id", encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mk(mut_mod, direction: str, action: str, *, target: str, payload=None):
    D = mut_mod.MutationDirection
    A = mut_mod.MutationAction
    return mut_mod.Mutation(
        direction=getattr(D, direction),
        action=getattr(A, action),
        target=target,
        payload=payload or {},
        provenance={"source": "effect-matrix-test"},
    )


def _event_files(tracker_dir: Path, local_id: str) -> list[Path]:
    return sorted((tracker_dir / local_id).glob("*.json"))


def _read_event(tracker_dir: Path, local_id: str, event_type: str) -> dict:
    for path in _event_files(tracker_dir, local_id):
        ev = json.loads(path.read_text())
        if ev.get("event_type") == event_type:
            return ev
    raise AssertionError(f"no {event_type} event in {tracker_dir / local_id}")


# ---------------------------------------------------------------------------
# The matrix: (direction, action) -> driver(handler, applier, mut_mod, repo_root)
#
# Every leaf accepts (mutation, *, client=None, repo_root=None) — inbound_create
# additionally accepts binding_store — so each driver invokes the handler pulled
# from _LEAVES uniformly. Each driver asserts the exact method/target/fields, the
# ApplyResult/follow-on, AND (where one exists) the error/exception path.
# ---------------------------------------------------------------------------

_MATRIX: dict[tuple[str, str], Callable] = {}


def _matrix(direction: str, action: str):
    def deco(fn: Callable) -> Callable:
        _MATRIX[(direction, action)] = fn
        return fn

    return deco


@_matrix("outbound", "create")
def _drive_outbound_create(handler, applier, mut_mod, repo_root):
    # Success: create_issue called with the exact payload dict; empty ApplyResult.
    client = MagicMock()
    client.create_issue.return_value = {"key": "PROJ-1"}
    payload = {"summary": "Login page", "key_hint": "PROJ-1"}
    mut = _mk(mut_mod, "outbound", "create", target="LOCAL-A", payload=payload)
    result = handler(mut, client=client, repo_root=repo_root)
    assert client.create_issue.call_count == 1
    assert client.create_issue.call_args.args == ({"summary": "Login page", "key_hint": "PROJ-1"},)
    client.delete_issue.assert_not_called()
    assert result.payload == {}
    assert result.direction == mut.direction and result.action == mut.action

    # Error path: create raises -> rollback delete_issue(key_hint) -> ORIGINAL reraises.
    err_client = MagicMock()
    err_client.create_issue.side_effect = RuntimeError("create boom")
    err_mut = _mk(
        mut_mod,
        "outbound",
        "create",
        target="LOCAL-B",
        payload={"summary": "x", "key_hint": "PROJ-9"},
    )
    with pytest.raises(RuntimeError, match="create boom"):
        handler(err_mut, client=err_client, repo_root=repo_root)
    err_client.delete_issue.assert_called_once()
    assert err_client.delete_issue.call_args.args == ("PROJ-9",)


@_matrix("outbound", "update")
def _drive_outbound_update(handler, applier, mut_mod, repo_root, monkeypatch):
    # Success: delegates to the ONE production applier update_one with a batch dict
    # carrying the exact target (key) and the exact changed fields.
    client = MagicMock()
    captured: dict = {}

    def fake_update_one(batch, cl, comment_errors=None):
        captured["batch"] = batch
        captured["client"] = cl
        return {"updated": True}

    monkeypatch.setattr(apply_outbound, "update_one", fake_update_one)
    mut = _mk(
        mut_mod,
        "outbound",
        "update",
        target="PROJ-7",
        payload={"changed_fields": {"summary": "New title"}},
    )
    result = handler(mut, client=client, repo_root=repo_root)
    assert captured["batch"]["key"] == "PROJ-7"
    assert captured["batch"]["fields"] == {"summary": "New title"}
    assert captured["client"] is client
    assert result.payload == {"update_result": {"updated": True}}
    assert "comment_errors" not in result.payload

    # Failure surface: a comment sub-op failure is surfaced (not swallowed).
    def fake_update_one_err(batch, cl, comment_errors=None):
        comment_errors.append("add_comment failed: 500")
        return {"updated": False}

    monkeypatch.setattr(apply_outbound, "update_one", fake_update_one_err)
    result2 = handler(mut, client=client, repo_root=repo_root)
    assert result2.payload["comment_errors"] == ["add_comment failed: 500"]


@_matrix("outbound", "delete")
def _drive_outbound_delete(handler, applier, mut_mod, repo_root):
    # Success: delete_issue(target); {"deleted": target}.
    client = MagicMock()
    client.delete_issue.return_value = None
    mut = _mk(mut_mod, "outbound", "delete", target="PROJ-3")
    result = handler(mut, client=client, repo_root=repo_root)
    assert client.delete_issue.call_args.args == ("PROJ-3",)
    assert result.payload == {"deleted": "PROJ-3"}

    # Error path A: 404 not-found is the desired post-state -> already_gone.
    client_404 = MagicMock()
    client_404.delete_issue.side_effect = JiraAPIError("gone", 404)
    result_404 = handler(mut, client=client_404, repo_root=repo_root)
    assert result_404.payload == {"already_gone": True}

    # Error path B: a non-404 (non-retriable 4xx) JiraAPIError propagates raw.
    client_400 = MagicMock()
    client_400.delete_issue.side_effect = JiraAPIError("bad request", 400)
    with pytest.raises(JiraAPIError):
        handler(mut, client=client_400, repo_root=repo_root)


@_matrix("outbound", "probe")
def _drive_outbound_probe(handler, applier, mut_mod, repo_root):
    # Success: get_issue(target); {"present": True, "issue": info}.
    client = MagicMock()
    client.get_issue.return_value = {"id": "10001"}
    mut = _mk(mut_mod, "outbound", "probe", target="PROJ-5")
    result = handler(mut, client=client, repo_root=repo_root)
    assert client.get_issue.call_args.args == ("PROJ-5",)
    assert result.payload == {"present": True, "issue": {"id": "10001"}}

    # Error path A: absence status codes -> present False.
    client_404 = MagicMock()
    client_404.get_issue.side_effect = JiraAPIError("gone", 404)
    result_404 = handler(mut, client=client_404, repo_root=repo_root)
    assert result_404.payload == {"present": False}

    # Error path B: an unexpected (non-retriable 4xx) status propagates raw.
    client_400 = MagicMock()
    client_400.get_issue.side_effect = JiraAPIError("bad request", 400)
    with pytest.raises(JiraAPIError):
        handler(mut, client=client_400, repo_root=repo_root)


@_matrix("outbound", "conflict")
def _drive_outbound_conflict(handler, applier, mut_mod, repo_root):
    # Success: add_comment(target, "...<reason>"); suppress_pair follow_on.
    client = MagicMock()
    mut = _mk(
        mut_mod,
        "outbound",
        "conflict",
        target="PROJ-8",
        payload={"reason": "dual-write drift", "local_id": "L1"},
    )
    result = handler(mut, client=client, repo_root=repo_root)
    assert client.add_comment.call_args.args[0] == "PROJ-8"
    assert "dual-write drift" in client.add_comment.call_args.args[1]
    assert result.payload["follow_on"] == {
        "kind": "suppress_pair",
        "local_id": "L1",
        "jira_key": "PROJ-8",
    }

    # Error path: the conflict comment is best-effort — a raising add_comment is
    # swallowed and the suppress_pair follow_on is STILL emitted (not lost).
    err_client = MagicMock()
    err_client.add_comment.side_effect = RuntimeError("comment boom")
    result2 = handler(mut, client=err_client, repo_root=repo_root)
    assert result2.payload["follow_on"]["jira_key"] == "PROJ-8"


@_matrix("inbound", "create")
def _drive_inbound_create(handler, applier, mut_mod, repo_root):
    # Success: writes a local CREATE event AND writes dedup markers back to Jira
    # (label + entity property) against the exact Jira key.
    client = MagicMock()
    mut = _mk(
        mut_mod,
        "inbound",
        "create",
        target="DIG-42",
        payload={"fields": {"summary": "Imported title", "issuetype": "Task", "priority": 2}},
    )
    result = handler(mut, client=client, repo_root=repo_root)
    local_id = "jira-dig-42"
    assert result.payload["local_id"] == local_id
    tracker = repo_root / ".tickets-tracker"
    create_ev = _read_event(tracker, local_id, "CREATE")
    assert create_ev["data"]["title"] == "Imported title"
    assert create_ev["data"]["ticket_type"] == "task"
    assert create_ev["data"]["priority"] == 2
    # Jira-side write-back: exact target + exact marker payloads.
    client.add_label.assert_called_once_with("DIG-42", f"rebar-id:{local_id}")
    client.set_entity_property.assert_called_once_with("DIG-42", "local_id", local_id)


@_matrix("inbound", "update")
def _drive_inbound_update(handler, applier, mut_mod, repo_root):
    # Seed a ticket dir via the create leaf, then update it.
    seed = _mk(
        mut_mod,
        "inbound",
        "create",
        target="DIG-43",
        payload={"fields": {"summary": "Original", "issuetype": "Task"}},
    )
    applier._LEAVES[(seed.direction, seed.action)](seed, repo_root=repo_root)

    mut = _mk(
        mut_mod,
        "inbound",
        "update",
        target="jira-dig-43",
        payload={"fields": {"summary": "Changed title"}},
    )
    result = handler(mut, repo_root=repo_root)
    assert result.payload["local_id"] == "jira-dig-43"
    tracker = repo_root / ".tickets-tracker"
    edits = [
        json.loads(p.read_text()) for p in _event_files(tracker, "jira-dig-43") if "EDIT" in p.name
    ]
    assert edits, "expected an EDIT event"
    assert edits[-1]["data"]["fields"]["title"] == "Changed title"


@_matrix("inbound", "delete")
def _drive_inbound_delete(handler, applier, mut_mod, repo_root):
    # Seed the ticket dir.
    seed = _mk(
        mut_mod,
        "inbound",
        "create",
        target="DIG-44",
        payload={"fields": {"summary": "seed", "issuetype": "Task"}},
    )
    applier._LEAVES[(seed.direction, seed.action)](seed, repo_root=repo_root)

    # Success (hard_delete branch): COMMENT event + exact outbound re-create follow-on.
    mut = _mk(
        mut_mod,
        "inbound",
        "delete",
        target="jira-dig-44",
        payload={"reason": "hard_delete"},
    )
    result = handler(mut, repo_root=repo_root)
    assert result.payload["branch"] == "hard_delete"
    assert result.payload["follow_on"] == {
        "direction": "outbound",
        "action": "create_after_hard_delete",
        "target": "jira-dig-44",
        "local_id": "jira-dig-44",
    }
    tracker = repo_root / ".tickets-tracker"
    assert any("COMMENT" in p.name for p in _event_files(tracker, "jira-dig-44"))

    # Error path: redirect onto an existing destination must raise (no silent skip
    # that would leave two dirs for one logical ticket).
    seed2 = _mk(
        mut_mod,
        "inbound",
        "create",
        target="DIG-45",
        payload={"fields": {"summary": "src", "issuetype": "Task"}},
    )
    applier._LEAVES[(seed2.direction, seed2.action)](seed2, repo_root=repo_root)
    (tracker / "jira-dig-999").mkdir()
    redirect = _mk(
        mut_mod,
        "inbound",
        "delete",
        target="jira-dig-45",
        payload={"reason": "redirect", "new_jira_key": "DIG-999"},
    )
    with pytest.raises(FileExistsError):
        handler(redirect, repo_root=repo_root)


@_matrix("inbound", "probe")
def _drive_inbound_probe(handler, applier, mut_mod, repo_root):
    # Seed a ticket dir so the acknowledgement COMMENT is written.
    seed = _mk(
        mut_mod,
        "inbound",
        "create",
        target="DIG-46",
        payload={"fields": {"summary": "seed", "issuetype": "Task"}},
    )
    applier._LEAVES[(seed.direction, seed.action)](seed, repo_root=repo_root)

    mut = _mk(mut_mod, "inbound", "probe", target="jira-dig-46")
    result = handler(mut, repo_root=repo_root)
    assert result.payload == {"local_id": "jira-dig-46", "probed": "jira-dig-46"}
    tracker = repo_root / ".tickets-tracker"
    comments = [
        json.loads(p.read_text())
        for p in _event_files(tracker, "jira-dig-46")
        if "COMMENT" in p.name
    ]
    assert any(
        "inbound probe acknowledged for jira-dig-46" in c["data"]["comment"] for c in comments
    )


@_matrix("inbound", "clean_label")
def _drive_inbound_clean_label(handler, applier, mut_mod, repo_root):
    # Success: exactly one remove_label(target, label) per rebar-id-* label,
    # non-rebar-id labels filtered defensively.
    client = MagicMock()
    mut = _mk(
        mut_mod,
        "inbound",
        "clean_label",
        target="PROJ-100",
        payload={"labels_to_remove": ["rebar-id-abc", "team-x", "rebar-id-xyz"]},
    )
    result = handler(mut, client=client, repo_root=repo_root)
    assert client.remove_label.call_count == 2
    assert client.remove_label.call_args_list[0].args == ("PROJ-100", "rebar-id-abc")
    assert client.remove_label.call_args_list[1].args == ("PROJ-100", "rebar-id-xyz")
    assert result.payload == {"removed": ["rebar-id-abc", "rebar-id-xyz"]}


@_matrix("inbound", "repair_property")
def _drive_inbound_repair_property(handler, applier, mut_mod, repo_root):
    # Success: set_issue_property(target, "local_id", local_id); status ok.
    client = MagicMock()
    mut = _mk(
        mut_mod,
        "inbound",
        "repair_property",
        target="DIG-X",
        payload={"local_id": "L7"},
    )
    result = handler(mut, client=client, repo_root=repo_root)
    client.set_issue_property.assert_called_once_with("DIG-X", "local_id", "L7")
    assert result.payload["status"] == "ok"
    assert result.payload["key"] == "DIG-X"

    # Error path: set_issue_property raises -> label-cleanup + schema_drift_signal.
    err_client = MagicMock()
    err_client.set_issue_property.side_effect = RuntimeError("property write failed")
    result2 = handler(mut, client=err_client, repo_root=repo_root)
    assert result2.payload["status"] == "repair_property_failed"
    assert result2.payload["follow_on"]["kind"] == "schema_drift_signal"
    err_client.remove_label.assert_called_once_with("DIG-X", "rebar-id-L7")


@_matrix("inbound", "conflict")
def _drive_inbound_conflict(handler, applier, mut_mod, repo_root):
    # Pure leaf (no client / no I/O): suppress_pair follow_on + a pending_bug_ticket
    # directive carrying the exact pair identity + reason.
    mut = _mk(
        mut_mod,
        "inbound",
        "conflict",
        target="DIG-7",
        payload={"local_id": "jira-dig-7", "reason": "dual-write divergence"},
    )
    result = handler(mut, repo_root=repo_root)
    assert result.payload["follow_on"] == {
        "kind": "suppress_pair",
        "local_id": "jira-dig-7",
        "jira_key": "DIG-7",
    }
    pbt = result.payload["pending_bug_ticket"]
    assert pbt["jira_key"] == "DIG-7"
    assert pbt["local_id"] == "jira-dig-7"
    assert "dual-write divergence" in pbt["description"]


# ---------------------------------------------------------------------------
# 1. Behavioral effect assertions — one parametrised case per leaf.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", sorted(_MATRIX))
def test_leaf_behavioral_effect(key, applier, mut_mod, fixture_repo, monkeypatch):
    """Drive the leaf from _LEAVES and assert its exact observable effect."""
    D = getattr(mut_mod.MutationDirection, key[0])
    A = getattr(mut_mod.MutationAction, key[1])
    handler = applier._LEAVES[(D, A)]
    driver = _MATRIX[key]
    # Only the outbound-update driver needs monkeypatch (to swap update_one); pass
    # it through when the driver declares it.
    import inspect

    if "monkeypatch" in inspect.signature(driver).parameters:
        driver(handler, applier, mut_mod, fixture_repo, monkeypatch)
    else:
        driver(handler, applier, mut_mod, fixture_repo)


# ---------------------------------------------------------------------------
# 2. Completeness — the matrix must cover every leaf in _LEAVES.
# ---------------------------------------------------------------------------


def test_matrix_covers_every_leaf(applier, mut_mod):
    """Every (direction, action) in _LEAVES must have a behavioural matrix entry.

    Derives the expected set FROM _LEAVES so a newly-added leaf with no
    behavioural coverage fails here (rather than silently shipping untested).
    """
    leaf_keys = {(d.value, a.value) for (d, a) in applier._LEAVES}
    matrix_keys = set(_MATRIX)
    missing = leaf_keys - matrix_keys
    extra = matrix_keys - leaf_keys
    assert not missing, f"leaves in _LEAVES with NO behavioural matrix entry: {sorted(missing)}"
    assert not extra, f"matrix entries with no matching leaf in _LEAVES: {sorted(extra)}"
    assert matrix_keys == leaf_keys
