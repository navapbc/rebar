#!/usr/bin/env python3
"""load_phase.py — reconcile_once's load phase, extracted from reconcile.py.

Ticket piscine-bullish-cowbird (module-size headroom): reconcile.py was at the
locked 800-line cap. ``load_snapshots`` (the sibling-module load + persist-flag +
sync-logger + local-tickets + binding-store + prev/curr-snapshot phase) and its
``handle_corrupt_snapshot`` abort helper moved here verbatim.

Dependency-injection, not a back-edge: several of ``load_snapshots``'s
collaborators (``_load``, ``_read_local_tickets``, ``ensure_selection_current``,
``narrow_selection_inputs``, ``_NoOpSyncLogger``, ``_handle_corrupt_snapshot``) are
monkeypatched by existing tests THROUGH ``reconcile.<name>`` and relied on to alter
this phase's behavior (see e.g. ``test_recovery_write_boundary.py``,
``test_bridge_selection_scope_heldout.py``, ``test_orchestrator_wiring.py``). A
plain function move would silently break those seams: Python resolves a bare name
inside a function body against the DEFINING module's globals, not the caller's, so
patching ``reconcile._read_local_tickets`` would have no effect on a copy of this
function living in a different module's namespace. ``reconcile.py`` therefore keeps
a thin ``_load_snapshots`` wrapper that resolves each of those names as bare names
in ITS OWN globals (picking up any monkeypatch) and forwards them here as keyword
arguments, so every existing patch keeps working unchanged.
``handle_corrupt_snapshot`` has no such external patch dependency (nothing patches
it to observe ``load_snapshots`` calling it), so it moved as a plain function and
reconcile.py aliases it directly.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

# Same standalone-load bootstrap idiom as reconcile.py: import lazy_load
# normally when package context exists, else load it by file path so this
# module keeps working when exec'd standalone (tests load reconciler siblings
# via spec_from_file_location).
try:
    from rebar_reconciler._loader import lazy_load
except ImportError:  # standalone load without package context
    _loader_key = "rebar_reconciler._loader"
    if _loader_key not in sys.modules:
        _loader_spec = importlib.util.spec_from_file_location(
            _loader_key, Path(__file__).parent / "_loader.py"
        )
        assert _loader_spec is not None and _loader_spec.loader is not None
        _loader_mod = importlib.util.module_from_spec(_loader_spec)
        sys.modules[_loader_key] = _loader_mod
        _loader_spec.loader.exec_module(_loader_mod)
    lazy_load = sys.modules[_loader_key].lazy_load


def handle_corrupt_snapshot(
    pass_id: str, repo_root: Path, prev_path: Path, _exc: Exception
) -> None:
    """Abort the pass on a corrupt / conflict-marked ``prev_snapshot.json``.

    Lifted out of the ``reconcile_once`` spine (the corrupt-snapshot abort): emit
    a loud operator ERROR, best-effort record a critical alert, then raise
    ``RuntimeError``. The pass must NEVER proceed with an unknown Jira state, so
    this always raises.
    """
    # SAFETY INVARIANT: a corrupt or conflict-marked prev_snapshot.json
    # must NEVER cause the pass to proceed with an unknown Jira comment
    # state.  If we continued with prev_snapshot={}, the inbound differ
    # would re-derive all create mutations (expensive but safe).  However,
    # the outbound differ uses curr_snapshot (the live fetch), not
    # prev_snapshot, for comment dedup — so comment mutations would be
    # correct IF we could reach that point.  The problem is we cannot
    # trust that even prev_snapshot corruption is the only issue; the
    # tickets branch may be in a partially-merged state that makes curr
    # state unknown too.  Abort the pass with a loud ERROR and alert.
    _alert_key = f"corrupt_prev_snapshot:{pass_id}"
    print(
        f"ERROR: prev_snapshot.json is corrupt or contains git conflict "
        f"markers and cannot be parsed. Aborting reconcile pass "
        f"'{pass_id}' to prevent emitting mutations against unknown "
        f"Jira state. File: {prev_path}. Error: {_exc}. "
        f"Recovery: resolve the merge conflict or delete the file to "
        f"force a full re-fetch on the next pass.",
        file=sys.stderr,
    )
    try:
        _alert_store = lazy_load(
            "rebar_reconciler.alert_store",
            "alert_store.py",
        )
        _alert_store.append(
            {
                "key": _alert_key,
                "severity": "critical",
                "reason": (f"prev_snapshot.json corrupt/unparseable at {prev_path}: {_exc}"),
                "pass_id": pass_id,
                "file": str(prev_path),
                "resolved": False,
                "timestamp_ns": __import__("time").time_ns(),
            },
            repo_root,
        )
    except Exception as _alert_exc:  # noqa: BLE001 — best-effort alert; original corruption still raises
        print(
            f"ERROR: alert_store write also failed ({_alert_exc}); "
            f"corruption event not persisted to bridge_alerts.",
            file=sys.stderr,
        )
    raise RuntimeError(
        f"Aborting reconcile pass '{pass_id}': prev_snapshot.json "
        f"is corrupt or contains git conflict markers at {prev_path}. "
        f"Original parse error: {_exc}. "
        f"Recovery: resolve the merge conflict or delete the file."
    ) from _exc


def load_snapshots(
    ctx: Any,
    *,
    load: Callable[[str, str], Any],
    read_local_tickets: Callable[..., list],
    ensure_selection_current: Callable[..., None],
    narrow_selection_inputs: Callable[..., tuple],
    no_op_sync_logger_cls: type,
    handle_corrupt_snapshot: Callable[[str, Path, Path, Exception], None] = handle_corrupt_snapshot,
) -> None:
    """Load phase: sibling modules + persist flag + sync logger + local tickets +
    binding store + the prev/curr snapshots (aborting via ``handle_corrupt_snapshot``
    on a corrupt prev_snapshot). Populates ctx for the diff/apply/persist phases.

    Every collaborator that an existing test monkeypatches via ``reconcile.<name>``
    (``_load``, ``_read_local_tickets``, ``ensure_selection_current``,
    ``narrow_selection_inputs``, ``_NoOpSyncLogger``) is threaded in as a keyword
    argument by reconcile.py's thin wrapper rather than imported here, so those
    patches keep taking effect unchanged.
    """
    pass_id = ctx.pass_id
    repo_root = ctx.repo_root
    target_mode = ctx.target_mode
    filter_local_ids = ctx.filter_local_ids
    selection_ids = ctx.selection_ids
    fetcher = load("reconcile_fetcher", "fetcher.py")
    differ = load("reconcile_differ", "differ.py")
    applier = load("reconcile_applier", "applier.py")
    invariants_mod = load("reconcile_invariants", "invariants.py")
    binding_store_mod = load("reconcile_binding_store", "binding_store.py")
    outbound_differ_mod = load("reconcile_outbound_differ", "outbound_differ.py")
    inbound_differ_mod = load("reconcile_inbound_differ", "inbound_differ.py")
    local_label_intent_mod = load("reconcile_local_label_intent", "local_label_intent.py")
    sync_logger_mod = load("reconcile_sync_logger", "sync_logger.py")

    # -----------------------------------------------------------------------
    # Persistence gating (ticket yaw-plait-doe).
    #
    # cap-0 dry-run/preview passes are documented as read-only: they
    # run the full differ COMPUTATION and PRODUCE the report, but must write
    # NOTHING to the local store. Every write point below is gated on `persist`.
    #
    # target_mode None defaults to LIVE → persists. dry-run/preview
    # → cap 0 → persist=False. bootstrap-* / live → non-zero/None cap → persist.
    # -----------------------------------------------------------------------
    mode_mod = load("rebar_reconciler.mode", "mode.py")
    if target_mode is None:
        persist = True
    else:
        persist = mode_mod.MODE_CAPS.get(target_mode) != 0

    # -----------------------------------------------------------------------
    # Read local tickets from the ticket CLI.
    # -----------------------------------------------------------------------
    local_tickets = read_local_tickets(repo_root, no_sync=(not persist) or bool(selection_ids))

    # -----------------------------------------------------------------------
    # Load and recover binding store.
    # -----------------------------------------------------------------------
    binding_store = binding_store_mod.load_binding_store(repo_root)
    if selection_ids:
        ensure_selection_current(selection_ids, local_tickets)

    # Create write-bearing pass artifacts only after the under-lock staleness check.
    log_path = repo_root / "bridge_state" / f"sync-log-{pass_id}.jsonl"
    sync_logger = sync_logger_mod.SyncLogger(log_path) if persist else no_op_sync_logger_cls()
    scoped_ids = selection_ids or filter_local_ids
    sync_logger.log(
        "sync_pass_start",
        pass_id=pass_id,
        mode=target_mode.value if target_mode else "live",
        filtered=bool(scoped_ids),
        filter_count=len(scoped_ids) if scoped_ids else 0,
    )
    # Interrupted-retirement repair (RP-02 S3 T2): after the under-lock staleness gate,
    # and BEFORE the first remote fetch below — so no pass can interleave fresh liveness
    # evidence for a retired issue with completing that issue's retirement.
    binding_store.repair_at_write_boundary(persist=persist, scoped=bool(scoped_ids))
    if scoped_ids:
        print(
            f"FILTERED PASS: scope restricted to {len(scoped_ids)} "
            f"local IDs — not a production reconciliation."
        )

    snapshots_dir = repo_root / "bridge_state" / "snapshots"
    snapshots_dir.mkdir(parents=True, exist_ok=True)

    # Previous snapshot: kept in the ticket store so it survives between GHA runs (the
    # commit-back step commits that directory; bridge_state/snapshots/ on the main-branch
    # worktree was ephemeral, so the differ re-derived every mutation each pass). RESOLVED,
    # not composed — this also seeds ``ctx.tracker_dir`` (consumed by run_differs).
    from rebar.config import tracker_dir as _resolve_tracker_dir

    tracker_dir = _resolve_tracker_dir(repo_root)
    prev_dir = tracker_dir / ".bridge_state"
    prev_dir.mkdir(parents=True, exist_ok=True)
    prev_path = prev_dir / "prev_snapshot.json"
    if prev_path.exists():
        try:
            prev_snapshot: dict = json.loads(prev_path.read_text())
        except (json.JSONDecodeError, ValueError, OSError) as _exc:
            # A corrupt / conflict-marked prev_snapshot must NEVER let the pass
            # proceed with an unknown Jira state — alert + abort (see helper).
            handle_corrupt_snapshot(pass_id, repo_root, prev_path, _exc)
    else:
        prev_snapshot = {}

    # Fetch current remote state. In no-write mode use compute_snapshot so no
    # snapshot file is written; the differ runs identically on curr_snapshot.
    if persist:
        curr_path = fetcher.fetch_snapshot(pass_id, repo_root)
        curr_snapshot: dict = json.loads(curr_path.read_text())
    else:
        curr_path = None
        curr_snapshot = fetcher.compute_snapshot(pass_id, repo_root)

    if selection_ids and ctx.selection_kind:
        local_tickets, prev_snapshot, curr_snapshot = narrow_selection_inputs(
            ctx.selection_kind,
            selection_ids,
            local_tickets,
            prev_snapshot,
            curr_snapshot,
            binding_store,
        )

    ctx.persist = persist
    ctx.fetcher = fetcher
    ctx.differ = differ
    ctx.applier = applier
    ctx.invariants_mod = invariants_mod
    ctx.binding_store_mod = binding_store_mod
    ctx.outbound_differ_mod = outbound_differ_mod
    ctx.inbound_differ_mod = inbound_differ_mod
    ctx.local_label_intent_mod = local_label_intent_mod
    ctx.sync_logger_mod = sync_logger_mod
    ctx.mode_mod = mode_mod
    ctx.sync_logger = sync_logger
    ctx.local_tickets = local_tickets
    ctx.binding_store = binding_store
    ctx.tracker_dir = tracker_dir
    ctx.prev_path = prev_path
    ctx.prev_snapshot = prev_snapshot
    ctx.curr_path = curr_path
    ctx.curr_snapshot = curr_snapshot
