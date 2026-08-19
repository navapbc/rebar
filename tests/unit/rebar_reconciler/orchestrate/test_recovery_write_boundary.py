"""Where interrupted-retirement repair is allowed to happen, and where it is not.

RP-02 S3 T2 (``flamboyant-possessive-blackbuck``). T1 built the repair; this module pins
the single place a pass is permitted to run it, which is a narrower question than "does
the repair work".

Two independent conditions have to hold, and they come from different places:

* **Late enough.** The pass must already own the exclusive lock, have cleared provider
  preflight, and have passed the under-lock staleness gate. Repairing before those is
  repairing on someone else's behalf, from a store nobody has agreed is current.
* **Early enough.** The repair must precede the pass's FIRST remote issue observation.
  This is the ordering that carries real weight: a tombstone is authoritative retirement
  intent, and a pass that fetched first could see the retired issue answer 200 and then
  complete a retirement in the same breath as fresh evidence that the issue is alive.
  Doing the repair first keeps those two facts from being interleaved, so the recorded
  decision always wins over a later observation rather than racing it.

The window between those two is narrow and, in the current spine, it does NOT contain the
operation config snapshot: ``bind_operation_runtime`` is composed after ``_load_snapshots``
returns and therefore after the fetch. The repair reads only local binding state, so it
has no need of that snapshot, and the pre-observation requirement wins. That is also why
the call is in ``reconcile.py`` rather than ``run_differs.py`` — by the time
``run_differs`` is invoked the fetch has already happened, so no position inside it can
satisfy the ordering.

The zero-write tests here are about THIS operation only. The pre-existing create-recovery
call keeps its own, looser gate (unscoped passes only, with no write-bearing condition);
that asymmetry is deliberate in this story and is filed separately as
``easygoing-tremendous-frogmouth``.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
_ENGINE = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler"


def _load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def reconcile_mod() -> Any:
    return _load_module("reconcile_write_boundary_under_test", _ENGINE / "reconcile.py")


@pytest.fixture(scope="module")
def mode_mod(reconcile_mod: Any) -> Any:
    """The mode module THROUGH reconcile's own loader, not a fresh copy.

    ``persist`` is derived from ``MODE_CAPS.get(target_mode) != 0``, a dict keyed on the
    ``Mode`` enum. Loading mode.py a second time under a different module name would build
    a second enum class, and a miss in that dict returns ``None`` — which compares
    unequal to 0 and would silently make a dry-run look WRITE-BEARING. The read-only tests
    would then pass for the wrong reason, or fail for one. Sharing the loader's instance
    removes the question.
    """
    return reconcile_mod._load("rebar_reconciler.mode", "mode.py")


# ---------------------------------------------------------------------------
# On-disk fixtures: the real retired-first overlap under a pass's repo root
# ---------------------------------------------------------------------------


def _canonical(payload: Any) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _bridge(repo_root: Path) -> Path:
    return repo_root / ".tickets-tracker" / ".bridge_state"


def _seed_overlap(repo_root: Path) -> None:
    """Write a live store and a tombstone that name the SAME identity.

    This is the state a crash between ``save_retired()`` and ``save()`` leaves. It is
    written directly rather than produced through a fault injection because what is under
    test here is the pass's ORDERING, not the classifier; the classifier's own oracles
    build the overlap through the real production route.
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
    """Run the load phase over ``repo_root`` and return the ordered event log.

    Only the two seams that would make this slow or non-hermetic are stubbed: the local
    ticket read (a CLI call) and the snapshot fetch (a network call). The binding store,
    the repository, the sync logger and the repair itself are all REAL, because the
    ordering claim is about where the real repair sits relative to the real fetch.
    """
    events: list[str] = []

    monkeypatch.setattr(reconcile_mod, "_read_local_tickets", lambda *a, **k: [])

    if stale_selection:

        def _stale(*_a: Any, **_k: Any) -> None:
            events.append("staleness-abort")
            raise reconcile_mod.SelectionStaleError("selection is stale")

        monkeypatch.setattr(reconcile_mod, "ensure_selection_current", _stale)
    else:
        monkeypatch.setattr(
            reconcile_mod,
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

    # Spy the module PRODUCTION reaches, not a second copy of the same file. Loading
    # binding_recovery.py under a fresh module name builds a DIFFERENT BindingRecovery
    # class, so patching that one patches a class nothing calls: the repair still runs,
    # no "repair" event is ever recorded, and the ordering assertion below becomes
    # unfalsifiable. binding_store.py resolves `from rebar_reconciler import
    # binding_recovery`, which conftest wires to the engine directory, so this is the
    # object that matters. Same hazard as the mode-enum note on the fixture above.
    sys.path.insert(0, str(_ENGINE.parent))
    from rebar_reconciler import binding_recovery as recovery

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
    except reconcile_mod.SelectionStaleError:
        pass
    return events


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------


def test_repair_runs_before_the_first_remote_observation(
    reconcile_mod: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The load-bearing ordering claim, asserted as a sequence rather than a pair of
    separate "did it happen" checks — only the ORDER rules out interleaving completion
    with fresh liveness evidence."""
    _seed_overlap(tmp_path)

    events = _drive_load_phase(reconcile_mod, monkeypatch, tmp_path)

    assert "repair" in events and "remote-fetch" in events
    assert events.index("repair") < events.index("remote-fetch")


def test_repair_runs_after_the_under_lock_staleness_gate(
    reconcile_mod: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The other end of the window: the staleness gate is a precondition, not a
    formality. A pass whose selection is stale has not established that its view is
    current, so it has no standing to repair anything."""
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


def test_a_reconcile_check_pass_leaves_the_overlap_alone(
    reconcile_mod: Any, mode_mod: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _seed_overlap(tmp_path)

    _drive_load_phase(
        reconcile_mod, monkeypatch, tmp_path, target_mode=mode_mod.Mode.RECONCILE_CHECK
    )

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
    """Construction happens on every read-only command, including ones that take no lock
    at all, so it must stay inert. This is why the repair is a pass-level call and not
    something the loader does on the way in."""
    _seed_overlap(tmp_path)
    sys.path.insert(0, str(_ENGINE.parent))
    from rebar_reconciler.binding_store import load_binding_store

    store = load_binding_store(tmp_path)

    assert store.is_retired("DIG-A") is True
    assert store.is_bound("loc-A") is True
    assert not _is_repaired(tmp_path)


# ---------------------------------------------------------------------------
# The guard is enforced in the owner, not merely at the call site
# ---------------------------------------------------------------------------


def _recovery_over(repo_root: Path) -> Any:
    sys.path.insert(0, str(_ENGINE.parent))
    from rebar_reconciler import binding_lifecycle, binding_recovery
    from rebar_reconciler.binding_repository import BindingRepository

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
    sys.path.insert(0, str(_ENGINE.parent))
    from rebar_reconciler import binding_recovery

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
    """A HISTORICAL overlap: the live record was re-confirmed to the same key and its
    absence counter cleared, which is exactly what an automatic 200 looks like. Neither
    is a revocation, so the tombstone still wins and the pass completes the retirement.

    The owner-level twins live in ``state/test_binding_recovery.py``; this proves the
    guarded entry point did not lose the behavior on the way through.
    """
    _seed_overlap(tmp_path)
    sys.path.insert(0, str(_ENGINE.parent))
    from rebar_reconciler.binding_store import load_binding_store

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
    """``unretire`` is the one documented revocation. After it there is no intent left to
    complete, so the live binding is ordinary work again and the pass must not touch it."""
    _seed_overlap(tmp_path)
    sys.path.insert(0, str(_ENGINE.parent))
    from rebar_reconciler.binding_store import load_binding_store

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
    """Because the repair precedes the fetch, a failed repair CANNOT have made a remote
    call — the ordering is what delivers that guarantee, not a try/except. The overlap
    also has to survive so a later pass can retry it."""
    _seed_overlap(tmp_path)
    sys.path.insert(0, str(_ENGINE.parent))
    from rebar_reconciler import binding_repository

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
    """A repair mutates durable state without anyone asking for it, so a silent one is
    indistinguishable from a store that was always coherent. The operator needs to be
    able to correlate the change with the pass that made it."""
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
    """A structural anchor, both halves, on the decision this task turned on.

    ``run_differs`` is invoked AFTER the snapshot fetch, so a repair placed there could
    never satisfy the pre-observation ordering — pinning its absence keeps a future edit
    from "tidying" the call into the diff phase beside the create-recovery call it
    superficially resembles, since the two have deliberately different boundaries.

    The PRESENCE half matters more, and for a specific reason. ``reconcile.py`` is at 799
    of the 800-line cap, so the next change that needs room there is forced into an
    extraction (RP-03 S5 `diamond-flavoured-esok` is already queued against this file).
    An extraction can relocate this call without breaking a single other assertion in the
    suite, and if it lands after the first remote fetch S3's whole ordering guarantee is
    silently void — the repair would still work, still be tested, and still be wrong.
    Whoever splits this file must keep the call after the under-lock staleness gate and
    before the fetch; the behavioural oracle for that is
    ``test_repair_runs_before_the_first_remote_observation`` in this module.
    """
    assert "repair_at_write_boundary" in (_ENGINE / "reconcile.py").read_text(encoding="utf-8")

    run_differs_source = (_ENGINE / "run_differs.py").read_text(encoding="utf-8")
    assert "repair_at_write_boundary" not in run_differs_source
    assert "complete_interrupted_retirements" not in run_differs_source
