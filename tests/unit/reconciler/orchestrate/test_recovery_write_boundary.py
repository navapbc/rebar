"""Interrupted-retirement repair runs once, under lock and after staleness validation
but before remote observation, only for whole-store write-bearing passes. Read-only
and narrowed passes leave overlap untouched; outcomes reach the operator.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
_ENGINE = REPO_ROOT / "src" / "rebar" / "reconciler"


@pytest.fixture(scope="module")
def reconcile_mod() -> Any:
    return importlib.import_module("rebar.reconciler.reconcile")


@pytest.fixture(scope="module")
def mode_mod(reconcile_mod: Any) -> Any:
    """Use reconcile's loaded Mode enum so MODE_CAPS lookups compare the same enum
    identity.
    """
    return reconcile_mod._load("rebar.reconciler.mode", "mode.py")


# ---------------------------------------------------------------------------
# On-disk fixtures: the real retired-first overlap under a pass's repo root
# ---------------------------------------------------------------------------


def _canonical(payload: Any) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _bridge(repo_root: Path) -> Path:
    return repo_root / ".tickets-tracker" / ".bridge_state"


def _seed_overlap(repo_root: Path) -> None:
    """Seed a live binding and tombstone for one identity, matching the
    interrupted-retirement state.
    """
    bridge = _bridge(repo_root)
    bridge.mkdir(parents=True, exist_ok=True)
    (bridge / "bindings.json").write_bytes(
        _canonical(
            {
                "version": 2,
                "bindings": {
                    "loc-A": {
                        "jira_key": "DIG-A",
                        "state": "confirmed",
                        "created_at": "2026-01-01T00:00:00Z",
                    }
                },
                "reverse": {"DIG-A": "loc-A"},
                "comment_ids": {},
            }
        )
    )
    (bridge / "bindings-retired.json").write_bytes(
        _canonical(
            {
                "version": 1,
                "retired": {
                    "DIG-A": {
                        "local_id": "loc-A",
                        "retired_at": "2026-01-02T00:00:00Z",
                        "absent_404_count": 3,
                        "last_jira_key": "DIG-A",
                    }
                },
            }
        )
    )


def _live(repo_root: Path) -> dict[str, Any]:
    return json.loads((_bridge(repo_root) / "bindings.json").read_text(encoding="utf-8"))


def _retired(repo_root: Path) -> dict[str, Any]:
    payload = json.loads((_bridge(repo_root) / "bindings-retired.json").read_text(encoding="utf-8"))
    retired: dict[str, Any] = payload["retired"]
    return retired


def _is_repaired(repo_root: Path) -> bool:
    live = _live(repo_root)
    return "loc-A" not in live["bindings"] and "DIG-A" not in live["reverse"]


# ---------------------------------------------------------------------------
# Driving the load phase with an event recorder
# ---------------------------------------------------------------------------


def _drive_load_phase(
    reconcile_mod: Any,
    monkeypatch: pytest.MonkeyPatch,
    repo_root: Path,
    *,
    target_mode: Any = None,
    selection_ids: set[str] | None = None,
    filter_local_ids: set[str] | None = None,
    stale_selection: bool = False,
) -> list[str]:
    """Drive the load phase with local-ticket and remote-fetch seams stubbed, recording
    the ordering of repair and observation.
    """
    events: list[str] = []

    pass_support = reconcile_mod._load("rebar.reconciler.pass_support", "pass_support.py")
    monkeypatch.setattr(pass_support, "_read_local_tickets", lambda *a, **k: [])

    if stale_selection:

        def _stale(*_a: Any, **_k: Any) -> None:
            events.append("staleness-abort")
            raise pass_support.SelectionStaleError("selection is stale")

        monkeypatch.setattr(pass_support, "ensure_selection_current", _stale)
    else:
        monkeypatch.setattr(
            pass_support,
            "ensure_selection_current",
            lambda *a, **k: events.append("staleness-ok"),
        )

    fetcher = reconcile_mod._load("reconcile_fetcher", "fetcher.py")

    def _fetch(pass_id: str, root: Path) -> Path:
        events.append("remote-fetch")
        path = Path(root) / "snap.json"
        path.write_text("{}", encoding="utf-8")
        return path

    def _compute(pass_id: str, root: Path) -> dict:
        events.append("remote-fetch")
        return {}

    monkeypatch.setattr(fetcher, "fetch_snapshot", _fetch)
    monkeypatch.setattr(fetcher, "compute_snapshot", _compute)

    # Spy the imported BindingRecovery class that binding_store actually calls;
    # loading a second module identity would make the ordering check vacuous.
    from rebar.reconciler import binding_recovery as recovery

    real_boundary = recovery.BindingRecovery.repair_at_write_boundary

    def _spy(inner: Any, **kwargs: Any) -> Any:
        events.append("repair")
        return real_boundary(inner, **kwargs)

    monkeypatch.setattr(recovery.BindingRecovery, "repair_at_write_boundary", _spy)

    ctx = reconcile_mod._PassContext(
        pass_id="pass-boundary",
        repo_root=repo_root,
        target_mode=target_mode,
        selection_ids=selection_ids,
        filter_local_ids=filter_local_ids,
        selection_kind="ticket" if selection_ids else None,
    )
    try:
        reconcile_mod._load_snapshots(ctx)
    except pass_support.SelectionStaleError:
        pass
    return events


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------


def test_repair_runs_before_the_first_remote_observation(
    reconcile_mod: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Assert repair precedes remote fetch in one ordered event sequence."""
    _seed_overlap(tmp_path)

    events = _drive_load_phase(reconcile_mod, monkeypatch, tmp_path)

    assert "repair" in events and "remote-fetch" in events
    assert events.index("repair") < events.index("remote-fetch")


def test_repair_runs_after_the_under_lock_staleness_gate(
    reconcile_mod: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A stale selection cannot repair durable state; under-lock validation must
    precede repair.
    """
    _seed_overlap(tmp_path)

    events = _drive_load_phase(
        reconcile_mod, monkeypatch, tmp_path, selection_ids={"loc-A"}, stale_selection=True
    )

    assert events == ["staleness-abort"]
    assert not _is_repaired(tmp_path)


def test_the_staleness_gate_precedes_the_repair_in_a_healthy_pass(
    reconcile_mod: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _seed_overlap(tmp_path)

    events = _drive_load_phase(reconcile_mod, monkeypatch, tmp_path, selection_ids={"loc-A"})

    assert events.index("staleness-ok") < events.index("repair")


# ---------------------------------------------------------------------------
# The write-bearing / scope guard, at the pass level
# ---------------------------------------------------------------------------


def test_a_live_unscoped_pass_repairs_the_overlap(
    reconcile_mod: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _seed_overlap(tmp_path)

    _drive_load_phase(reconcile_mod, monkeypatch, tmp_path)

    assert _is_repaired(tmp_path)
    assert "DIG-A" in _retired(tmp_path)


def test_a_dry_run_pass_leaves_the_overlap_alone(
    reconcile_mod: Any, mode_mod: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A cap-0 mode is documented read-only. Repair is a write, so it does not happen —
    the overlap survives to be repaired by the next write-bearing pass."""
    _seed_overlap(tmp_path)

    _drive_load_phase(reconcile_mod, monkeypatch, tmp_path, target_mode=mode_mod.Mode.DRY_RUN)

    assert not _is_repaired(tmp_path)


def test_a_selected_pass_leaves_the_overlap_alone(
    reconcile_mod: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Repair is a WHOLE-STORE side effect, so any narrowing suppresses it: a pass asked
    to touch one ticket must not quietly rewrite bindings for others."""
    _seed_overlap(tmp_path)

    _drive_load_phase(reconcile_mod, monkeypatch, tmp_path, selection_ids={"loc-Z"})

    assert not _is_repaired(tmp_path)


def test_a_filtered_pass_leaves_the_overlap_alone(
    reconcile_mod: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _seed_overlap(tmp_path)

    _drive_load_phase(reconcile_mod, monkeypatch, tmp_path, filter_local_ids={"loc-Z"})

    assert not _is_repaired(tmp_path)


def test_merely_constructing_the_store_repairs_nothing(tmp_path: Path) -> None:
    """Store construction is used by unlocked reads and must leave interrupted
    retirement untouched.
    """
    _seed_overlap(tmp_path)
    from rebar.reconciler.binding_store import load_binding_store

    store = load_binding_store(tmp_path)

    assert store.is_retired("DIG-A") is True
    assert store.is_bound("loc-A") is True
    assert not _is_repaired(tmp_path)


# ---------------------------------------------------------------------------
# The guard is enforced in the owner, not merely at the call site
# ---------------------------------------------------------------------------


def _recovery_over(repo_root: Path) -> Any:
    from rebar.reconciler import binding_lifecycle, binding_recovery
    from rebar.reconciler.binding_repository import BindingRepository

    repo = BindingRepository(repo_root / ".tickets-tracker")
    return binding_recovery.BindingRecovery(repo, binding_lifecycle.BindingLifecycle(repo))


@pytest.mark.parametrize(
    ("persist", "scoped"),
    [(False, False), (False, True), (True, True)],
)
def test_the_guard_refuses_every_non_write_bearing_combination(
    tmp_path: Path, persist: bool, scoped: bool
) -> None:
    """Exhaustive over the matrix, because the guard is one boolean expression and an
    inverted operand would still pass a single-case test."""
    _seed_overlap(tmp_path)

    outcome = _recovery_over(tmp_path).repair_at_write_boundary(persist=persist, scoped=scoped)

    assert (outcome.completed, outcome.aborted) == ((), ())
    assert not _is_repaired(tmp_path)


def test_the_guard_admits_the_write_bearing_unscoped_case(tmp_path: Path) -> None:
    _seed_overlap(tmp_path)

    outcome = _recovery_over(tmp_path).repair_at_write_boundary(persist=True, scoped=False)

    assert [r.jira_key for r in outcome.completed] == ["DIG-A"]
    assert _is_repaired(tmp_path)


def test_a_refused_guard_does_no_classification_at_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refusing must be free, not merely harmless. Classifying and then discarding the
    answer would walk every tombstone in the retired file on every read-only command."""
    _seed_overlap(tmp_path)
    from rebar.reconciler import binding_recovery

    calls: list[int] = []
    real = binding_recovery.classify_interrupted_retirements
    monkeypatch.setattr(
        binding_recovery,
        "classify_interrupted_retirements",
        lambda *a, **k: (calls.append(1), real(*a, **k))[1],
    )

    _recovery_over(tmp_path).repair_at_write_boundary(persist=False, scoped=False)

    assert calls == []


# ---------------------------------------------------------------------------
# Tombstone authority, at the pass level (AC5 / AC6)
# ---------------------------------------------------------------------------


def test_a_reconfirmed_and_cleared_overlap_still_completes_at_the_boundary(
    reconcile_mod: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Reconfirming a live key or clearing its absence does not revoke retirement; the
    tombstone still wins at the write boundary.
    """
    _seed_overlap(tmp_path)
    from rebar.reconciler.binding_store import load_binding_store

    store = load_binding_store(tmp_path)
    store.bind_confirm("loc-A", "DIG-A")
    store.clear_absent("DIG-A")
    store.save()
    assert not _is_repaired(tmp_path)

    _drive_load_phase(reconcile_mod, monkeypatch, tmp_path)

    assert _is_repaired(tmp_path)
    assert "DIG-A" in _retired(tmp_path)


def test_an_explicitly_unretired_identity_is_not_repaired_at_the_boundary(
    reconcile_mod: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Explicit unretire revokes the tombstone, leaving the live binding untouched by
    repair.
    """
    _seed_overlap(tmp_path)
    from rebar.reconciler.binding_store import load_binding_store

    assert load_binding_store(tmp_path).unretire("DIG-A") is True

    _drive_load_phase(reconcile_mod, monkeypatch, tmp_path)

    assert not _is_repaired(tmp_path)
    assert _live(tmp_path)["bindings"]["loc-A"]["jira_key"] == "DIG-A"


# ---------------------------------------------------------------------------
# Failure containment and observability
# ---------------------------------------------------------------------------


def test_a_repair_failure_happens_before_any_remote_call_and_keeps_the_overlap(
    reconcile_mod: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Failed repair happens before remote calls and retains the overlap for a later
    pass.
    """
    _seed_overlap(tmp_path)
    from rebar.reconciler import binding_repository

    real_replace = binding_repository.os.replace

    def _fail(src: Any, dst: Any) -> Any:
        if Path(dst).name == "bindings.json":
            raise OSError("replace onto bindings.json failed")
        return real_replace(src, dst)

    monkeypatch.setattr(binding_repository.os, "replace", _fail)

    events = _drive_load_phase(reconcile_mod, monkeypatch, tmp_path)

    assert events.index("repair") < events.index("remote-fetch")
    assert not _is_repaired(tmp_path)
    assert _retired(tmp_path)["DIG-A"]["local_id"] == "loc-A"


def test_a_completed_repair_is_reported_to_the_operator(
    reconcile_mod: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Report a durable repair with its pass so the operator can explain the state
    change.
    """
    _seed_overlap(tmp_path)

    _drive_load_phase(reconcile_mod, monkeypatch, tmp_path)

    captured = capsys.readouterr()
    # Naming the KEY is not enough: the key appears in both the completed and the refused
    # line, so a key-only assertion passes even if a completion is reported as a refusal.
    assert "retirement_repair_completed" in captured.err
    assert "DIG-A" in captured.err
    assert "retirement_repair_refused" not in captured.err


def test_a_refused_repair_is_reported_to_the_operator(
    reconcile_mod: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A refusal is the finding that matters most: an inconsistent store that nobody is
    going to fix by itself. Reporting only successes would hide it forever."""
    bridge = _bridge(tmp_path)
    bridge.mkdir(parents=True, exist_ok=True)
    (bridge / "bindings.json").write_bytes(
        _canonical(
            {
                "version": 2,
                "bindings": {"loc-A": {"jira_key": "DIG-OTHER", "state": "confirmed"}},
                "reverse": {"DIG-OTHER": "loc-A"},
                "comment_ids": {},
            }
        )
    )
    (bridge / "bindings-retired.json").write_bytes(
        _canonical({"version": 1, "retired": {"DIG-A": {"local_id": "loc-A"}}})
    )

    _drive_load_phase(reconcile_mod, monkeypatch, tmp_path)

    captured = capsys.readouterr()
    assert "retirement_repair_refused" in captured.err
    assert "forward_key_mismatch" in captured.err
    assert "retirement_repair_completed" not in captured.err


def test_a_healthy_store_reports_nothing_about_repair(
    reconcile_mod: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Silence on the happy path. A per-pass line saying "nothing to repair" would train
    operators to ignore the one line that matters."""
    bridge = _bridge(tmp_path)
    bridge.mkdir(parents=True, exist_ok=True)
    (bridge / "bindings.json").write_bytes(
        _canonical({"version": 2, "bindings": {}, "reverse": {}, "comment_ids": {}})
    )

    _drive_load_phase(reconcile_mod, monkeypatch, tmp_path)

    captured = capsys.readouterr()
    assert "retirement" not in (captured.out + captured.err).lower()


# ---------------------------------------------------------------------------
# run_differs.py is not the invocation site
# ---------------------------------------------------------------------------


def test_the_repair_is_invoked_from_reconcile_and_not_from_run_differs() -> None:
    """Require the repair call in load_phase.py and its absence from
    run_differs.py; the load phase precedes fetch.
    """
    assert "repair_at_write_boundary" in (_ENGINE / "load_phase.py").read_text(encoding="utf-8")

    run_differs_source = (_ENGINE / "run_differs.py").read_text(encoding="utf-8")
    assert "repair_at_write_boundary" not in run_differs_source
    assert "complete_interrupted_retirements" not in run_differs_source
