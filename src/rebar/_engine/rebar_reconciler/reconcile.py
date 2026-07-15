#!/usr/bin/env python3
"""reconcile.py — one-pass orchestrator: fetch → diff → apply.

reconcile_once(pass_id) wires the three reconciler stages into a single
idempotent pass.  Two consecutive calls with an unchanged remote produce
mutation_count=0 on both passes (second call sees prev==curr snapshot).
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class StatusMappingError(Exception):
    """Names a mutation referencing a status that ``config.local_to_jira_status``
    maps in NEITHER direction — i.e. the value is neither a local-status key nor a
    Jira workflow status value. (Outbound mutations may carry either shape, so a Jira
    status added without a corresponding reconciler mapping trips this — the offending
    value is not necessarily a local status.) The preflight scan NO LONGER raises this
    (reconciler-abort-isolation): it now warns non-fatally and lets the applier record
    the offending mutation as a per-mutation failure instead of aborting the pass. The
    class stays defined for references (e.g. tests) that still name it."""


def preflight_status_mapping(mutations) -> None:
    """Scan update mutations for statuses absent from
    ``config.local_to_jira_status`` and WARN (non-fatally) on each.

    Facet 3 (reconciler-abort-isolation): this scan used to RAISE
    :class:`StatusMappingError` on the first unmapped status, aborting the whole
    pass before any mutation was applied. It now logs a warning to stderr for
    each offending mutation and returns normally, so the mutation flows to the
    applier and is recorded there as a per-mutation failure (fail-loud) instead
    of taking down the entire pass. :class:`StatusMappingError` remains defined
    for other references.

    An empty mapping disables the scan (kill-switch). Non-update mutations,
    inbound mutations, and mutations whose ``fields`` payload does not include a
    ``status`` key are ignored.
    """
    cfg = _load("reconcile_config", "config.py")
    mapping = getattr(cfg, "local_to_jira_status", {}) or {}
    if not mapping:
        return  # kill-switch — empty mapping disables preflight
    for m in mutations:
        # Mutations may be plain dicts (current schema) or objects with an
        # ``.action`` attribute (forward-compat). Normalise to a string action
        # and direction.
        action_attr = getattr(m, "action", None)
        if action_attr is not None:
            action = getattr(action_attr, "value", action_attr)
            fields = getattr(m, "fields", None) or getattr(m, "payload", None) or {}
            target = getattr(m, "target", getattr(m, "key", None))
            direction_attr = getattr(m, "direction", None)
            direction = (
                getattr(direction_attr, "value", direction_attr)
                if direction_attr is not None
                else None
            )
        else:
            action = m.get("action")
            fields = m.get("fields") or m.get("payload") or {}
            target = m.get("key") or m.get("local_id")
            direction = m.get("direction")
        if action != "update":
            continue
        # Bug 85a1: preflight validates *local* status names against the
        # local→jira mapping. Inbound mutations carry Jira's status (either
        # the raw REST dict or — post-normalisation — the Jira-side name),
        # which is the VALUE side of the mapping, not the KEY side.
        # Iterating inbound mutations through this check produces spurious
        # ``local status 'To Do' not in local_to_jira_status mapping`` errors
        # that abort the entire pass. Skip inbound entries; only outbound
        # mutations populate ``fields.status`` with a local-status key.
        if direction == "inbound":
            continue
        if not isinstance(fields, dict):
            continue
        raw_status = fields.get("status")
        # Bug 85a1: inbound and outbound paths both feed mutations through
        # this preflight. Outbound payloads carry the local status STRING
        # ("open", "in_progress", ...); inbound payloads carry Jira's
        # raw REST status DICT ({"name": "To Do", "id": ..., ...}). The
        # original ``status not in mapping`` check failed closed for dict
        # values with TypeError: unhashable type: 'dict'. Normalise dicts to
        # the ``.name`` field before lookup so the preflight is shape-tolerant.
        if isinstance(raw_status, dict):
            status = raw_status.get("name") or ""
        else:
            status = raw_status
        # Bug 85a1: outbound mutations may carry either:
        #   - a LOCAL status string ("open", "in_progress") — when the
        #     mutation originates from a path that hasn't translated yet;
        #   - or a JIRA status string ("To Do", "In Progress") — when the
        #     differ already mapped local→jira via _LOCAL_TO_JIRA_STATUS
        #     (outbound_differ._map_local_to_jira_fields:107).
        # Accept either by checking presence in mapping KEYS (local names)
        # OR VALUES (jira names). The preflight purpose is to catch
        # *unmapped* statuses before the applier dispatch; both shapes are
        # legitimately-mapped values.
        if status and status not in mapping and status not in set(mapping.values()):
            # Facet 3 (reconciler-abort-isolation): this preflight used to RAISE
            # StatusMappingError here, which aborted the ENTIRE pass on the FIRST
            # unmapped status — before ANY mutation was applied. That turned one
            # misconfigured mutation into a whole-pass outage. Downgrade to
            # NON-FATAL: log a warning to stderr naming the offending status +
            # target and RETURN NORMALLY, letting the mutation flow to the applier
            # where the per-mutation backstop (_apply_one) records it as a
            # per-mutation failure (so it still counts toward fail-loud / a
            # non-zero exit). The StatusMappingError class stays defined for other
            # references; the empty-mapping kill-switch and the inbound-skip above
            # are preserved. (Message framing per c672-5111-8201-4fa7: the value may
            # originate on the Jira side, so it is not necessarily a local status.)
            print(  # noqa: T201
                f"reconcile: preflight — status {status!r} is not mapped in "
                f"local_to_jira_status (neither a local-status key nor a Jira "
                f"workflow status value; target={target}); NOT aborting the pass "
                f"— the applier will record it as a per-mutation failure",
                file=sys.stderr,
            )


def _commit_binding_store_snapshot(
    binding_store: Any,
    repo_root: Path,
    pass_id: str,
) -> bool:
    """Commit the binding-store snapshot to the tickets orphan branch.

    Bug: binding_store.save() only writes bindings.json to the working-tree
    filesystem.  When the ticket-CLI's _push_tickets_branch() runs between
    reconciler passes and merges origin/tickets, the un-committed local copy
    of bindings.json is silently overwritten by the version committed by a
    concurrent GHA run — causing the NEXT reconciler pass to see previously-
    bound tickets as unbound, generating outbound CREATE mutations instead of
    UPDATE mutations and producing a no-op dedup-skip rather than field updates.

    Fix: after every successful binding_store.save(), git-stage the file and
    commit it to the tickets orphan branch inside the .tickets-tracker worktree.
    This mirrors what the GHA workflow's "commit-back" step does via
    ``git add -A``, but runs inline so local probe runs that don't go through
    GHA also get durable bindings.

    Returns:
        True  — commit succeeded (or nothing to commit — bindings already current).
        False — a subprocess error occurred; bindings persisted to filesystem only.

    Degrades gracefully: any subprocess error (git not available, tickets branch
    not checked out, no bindings path, etc.) is caught and logged to stderr;
    the reconciler pass continues and the next GHA commit-back will persist the
    bindings as normal.  The caller must NOT abort on False — commit failure
    must never break the sync pass.
    """
    from rebar_reconciler import git_adapter

    tracker_dir = repo_root / git_adapter.TRACKER_DIR
    # Bug 1e08: stage BOTH the live store and the retired-binding store. The
    # absence-lifecycle GC writes bindings-retired.json; a retirement-only pass
    # must also be committed (else a soft-deleted binding is silently lost on
    # the next ``git merge origin/tickets``).
    _rel_files = [git_adapter.BINDINGS_FILE, git_adapter.BINDINGS_RETIRED_FILE]
    _existing_rel = [rel for rel in _rel_files if (tracker_dir / rel).exists()]
    if not _existing_rel:
        return True  # Nothing to commit — not a failure

    try:
        # Stage only our two state files (never git add -A: avoid staging
        # unrelated working-tree changes in the tickets worktree).
        git_adapter.add(tracker_dir, *_existing_rel)
        # Check if there is actually a diff to commit (idempotent).
        staged_names = git_adapter.diff_cached_names(tracker_dir)
        # PER-FILE idempotency (bug 1e08): the prior substring test
        # ``"bindings.json" not in status.stdout`` does NOT match
        # ``bindings-retired.json`` as a distinct file, so a retirement-only
        # change (only bindings-retired.json staged) would be silently skipped.
        # Match on basename membership over the staged-file lines instead.
        _staged_basenames = {
            os.path.basename(line.strip()) for line in staged_names.splitlines() if line.strip()
        }
        _tracked_basenames = {
            os.path.basename(git_adapter.BINDINGS_FILE),
            os.path.basename(git_adapter.BINDINGS_RETIRED_FILE),
        }
        if not (_tracked_basenames & _staged_basenames):
            return True  # Already up-to-date; nothing to commit.
        git_adapter.commit(
            tracker_dir,
            f"reconciler: persist binding-store snapshot [pass {pass_id}]",
            no_verify=True,
            quiet=True,
        )
        return True
    except Exception as exc:  # noqa: BLE001 — fail-open: return False, log + alert, FS copy persists
        print(  # noqa: T201
            f"reconcile: binding-store commit to tickets branch failed "
            f"({exc!r}); bindings saved to filesystem only — "
            f"GHA commit-back will persist them on next run.",
            file=sys.stderr,
        )
        # Append an alert so operators see the failure in bridge_alerts.
        _commit_alert_key = f"binding-commit-failure:{pass_id}"
        try:
            _alert_store = _load(
                "rebar_reconciler.alert_store",
                "alert_store.py",
            )
            if not _alert_store.is_deduped(_commit_alert_key, repo_root):
                _alert_store.append(
                    {
                        "key": _commit_alert_key,
                        "severity": "error",
                        "reason": (
                            "binding-store commit to tickets branch failed; "
                            "bindings at risk of clobber on next git merge origin/tickets"
                        ),
                        "pass_id": pass_id,
                        "resolved": False,
                        "timestamp_ns": __import__("time").time_ns(),
                    },
                    repo_root,
                )
        except Exception as _alert_exc:  # noqa: BLE001 — best-effort alert write; must not mask commit failure
            print(  # noqa: T201
                f"ERROR: alert_store write also failed ({_alert_exc}); "
                f"binding-commit failure not persisted to bridge_alerts.",
                file=sys.stderr,
            )
        return False


# ``lazy_load`` centralizes the by-path sibling-loader idiom (rebar_reconciler/
# _loader.py). Import it normally when package context exists, else bootstrap it
# by file path — this module is itself exec'd standalone via
# spec_from_file_location in tests.
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
        _loader_spec.loader.exec_module(_loader_mod)  # type: ignore[union-attr]
    lazy_load = sys.modules[_loader_key].lazy_load


def _load(name: str, relpath: str):
    """Load a sibling module by relative file path, registering it in sys.modules.

    Returns the cached module when ``name`` is already in ``sys.modules``;
    this allows test fixtures to pre-register patched modules and have
    ``reconcile_once`` reuse them rather than loading fresh copies. Delegates to
    the shared ``lazy_load`` helper (the package-wide by-path loader).
    """
    return lazy_load(name, relpath)


def _audit_log_probe(branch_label: str, issue_key: str, detail: dict | None = None) -> None:
    """Write a single audit-log entry to stderr for log-only probe branches.

    Used by :func:`route_inbound_probe` for branches that produce no follow-on
    mutation (``trash_restore`` / PRESENT_RESOLVED and ``unreachable`` /
    UNREACHABLE) so that the probe outcome is still durably observable in
    pass logs.
    """
    detail_str = "" if not detail else f" detail={detail!r}"
    print(  # noqa: T201
        f"inbound_probe: branch={branch_label} key={issue_key}{detail_str}",
        file=sys.stderr,
    )


def route_inbound_probe(mutation: Any, probe_result: Any) -> list[Any] | None:
    """Route an (inbound, probe) Mutation to a branch-specific follow-on.

    Branches:
      * ARCHIVED_OR_MOVED → ``hard_delete`` — emit ``(inbound, delete, target)``
        follow-on targeting the local jira-<key> partner. Provenance records
        the original probe target and the probe status_code.
      * PRESENT_RESOLVED  → ``trash_restore`` — NO follow-on; write one audit
        log entry with branch=``trash_restore`` and the issue key.
      * PRESENT_FILTERED  → currently no emission. The basic 4-branch
        classifier does not distinguish ``project_move`` from generic
        ``trash_restore``-filtered; a future enhancement may inspect
        ``probe_result.detail`` for a ``new_project_key`` signal and emit a
        reparent follow-on. For now, log and emit no follow-on.
      * UNREACHABLE       → NO follow-on; audit log entry with
        branch=``unreachable``.

    Args:
        mutation:     The (inbound, probe) Mutation under inspection.
        probe_result: An :class:`inbound_probe.ProbeResult`.

    Returns:
        A list of follow-on Mutations (possibly empty) for branches that
        emit follow-ons, or ``None`` for log-only branches.
    """
    # Lazy-load probe + mutation modules to avoid import cycles at module top.
    probe_mod = _load("inbound_probe", "inbound_probe.py")
    mut_mod = _load("reconcile_mutation", "mutation.py")

    branch = probe_result.branch
    target = probe_result.issue_key

    if branch == probe_mod.ProbeBranch.ARCHIVED_OR_MOVED:
        follow_on = mut_mod.Mutation(
            direction=mut_mod.MutationDirection.inbound,
            action=mut_mod.MutationAction.delete,
            target=target,
            payload={
                "reason": "hard_delete",
                "probe_detail": dict(probe_result.detail),
            },
            provenance={
                "source": "inbound_probe_dispatch",
                "branch": "hard_delete",
                "origin_target": getattr(mutation, "target", target),
            },
        )
        return [follow_on]

    if branch == probe_mod.ProbeBranch.PRESENT_RESOLVED:
        _audit_log_probe("trash_restore", target, dict(probe_result.detail))
        return None

    if branch == probe_mod.ProbeBranch.PRESENT_FILTERED:
        # No follow-on for generic filtered branch under the 4-branch classifier.
        # Future enhancement: detect project_move via probe_result.detail.
        _audit_log_probe("present_filtered", target, dict(probe_result.detail))
        return None

    if branch == probe_mod.ProbeBranch.UNREACHABLE:
        _audit_log_probe("unreachable", target, dict(probe_result.detail))
        return None

    # Defensive fallback for unknown branch values.
    _audit_log_probe(f"unknown:{branch!r}", target, dict(probe_result.detail))
    return None


def _read_local_tickets(repo_root: Path, *, no_sync: bool = False) -> list[dict]:
    """Read local tickets from the ticket CLI, falling back to empty list.

    In production the ticket CLI is the ``rebar`` dispatcher (``rebar list``),
    self-resolved via :func:`in_process_cli`. If the CLI is unavailable (unit
    tests, minimal environments), return an empty list with a warning on stderr.

    Passes ``--full`` because ``rebar list`` is lean by default (it omits the
    ``description``/``comments`` bodies) and the outbound differ compares those
    bodies against Jira — a lean read would compute spurious mutations.

    ``no_sync=True`` sets REBAR_SYNC_PULL=off for the subprocess so the read does not
    trigger the tickets-branch fetch/reconverge (a git working-tree mutation).
    Cap-0 reconcile passes (dry-run/reconcile-check) pass this so a no-write
    pass stays literally no-write on the local git tree (review M3).
    """
    import os as _os  # local import to avoid top-level dep
    import subprocess as _sp  # local import to avoid top-level dep

    from rebar._engine import in_process_cli

    cli = Path(in_process_cli())
    if not cli.exists():
        print(  # noqa: T201
            "reconcile: ticket CLI not found — local_tickets=[]",
            file=sys.stderr,
        )
        return []
    _env = dict(_os.environ, REBAR_SYNC_PULL="off") if no_sync else None
    try:
        result = _sp.run(
            [str(cli), "list", "--full"],
            capture_output=True,
            text=True,
            cwd=str(repo_root),
            timeout=60,
            env=_env,
        )
        if result.returncode != 0:
            print(  # noqa: T201
                f"reconcile: ticket CLI exited {result.returncode} — local_tickets=[]",
                file=sys.stderr,
            )
            return []
        return json.loads(result.stdout)
    except Exception as exc:  # noqa: BLE001 — fail-open: log and return empty local_tickets list
        print(  # noqa: T201
            f"reconcile: ticket CLI failed ({exc}) — local_tickets=[]",
            file=sys.stderr,
        )
        return []


def _build_filter_target_set(
    filter_local_ids: set[str],
    binding_store: Any,
) -> set[str]:
    """Build the full set of targets that match *filter_local_ids*.

    Returns the union of the local IDs themselves and their bound Jira keys
    (if any).  A mutation matches the filter when its ``target``,
    ``provenance.local_id``, or ``provenance.jira_key`` intersects this set.
    """
    targets = set(filter_local_ids)
    for lid in filter_local_ids:
        jira_key = binding_store.get_jira_key(lid)
        if jira_key:
            targets.add(jira_key)
    return targets


def _mutation_matches_filter(mutation: Any, target_set: set[str]) -> bool:
    """Return True if *mutation* targets a ticket in *target_set*."""
    if getattr(mutation, "target", None) in target_set:
        return True
    prov = getattr(mutation, "provenance", None) or {}
    if isinstance(prov, Mapping):
        if prov.get("local_id") in target_set:
            return True
        if prov.get("jira_key") in target_set:
            return True
    return False


def _build_plan_entries(mutations) -> list[dict]:
    """Build a list of per-mutation plan entries for the no-write report.

    Each entry carries enough detail to be a useful plan:
    ``{direction, action, target, local_id}``. Tolerates both typed Mutation
    objects (``.direction``/``.action`` enums) and legacy dict mutations.
    """
    entries: list[dict] = []
    for m in mutations:
        direction = getattr(m, "direction", None)
        action = getattr(m, "action", None)
        if direction is not None or action is not None:
            d = str(getattr(direction, "value", direction) or "")
            a = str(getattr(action, "value", action) or "")
            target = getattr(m, "target", None)
            prov = getattr(m, "provenance", None) or {}
            local_id = prov.get("local_id") if isinstance(prov, Mapping) else None
        else:
            d = str(m.get("direction", "") or "")
            a = str(m.get("action", "") or "")
            target = m.get("key") or m.get("target")
            local_id = m.get("local_id")
        entries.append(
            {
                "direction": d,
                "action": a,
                "target": target,
                "local_id": local_id,
            }
        )
    return entries


class _NoOpSyncLogger:
    """No-op stand-in for SyncLogger used by cap-0 (no-write) passes.

    Implements the full surface ``reconcile_once`` calls on a sync logger
    (``log`` and ``close``) but writes nothing — so a dry-run / reconcile-check
    pass produces no ``sync-log-<pass>.jsonl`` file.
    """

    def log(self, *args, **kwargs) -> None:  # noqa: D401
        return None

    def close(self) -> None:
        return None


@dataclass
class _PassContext:
    """Mutable per-pass state threaded through reconcile_once's phase helpers.

    reconcile_once is a thin sequencer over _load_snapshots -> run_differs ->
    _apply_mutations -> _persist_and_log; each phase reads the fields it needs
    and writes back the ones it produces. Carrying the ~30 threaded values on one
    object (rather than as positional params) keeps each phase independently
    callable + unit-testable while preserving the single-pass idempotent contract.
    """

    # inputs (set at construction)
    pass_id: str
    repo_root: Path
    target_mode: Any = None
    filter_local_ids: set[str] | None = None
    # optional per-mutation lost-lease checkpoint (epic dust-troth-naval): a
    # zero-arg callable the applier invokes before each mutation; it raises
    # (ReconcileLockLost) if the ref-lock heartbeat lost the lease. None = no-op.
    abort_check: Any = None
    # populated by _load_snapshots
    persist: bool = True
    fetcher: Any = None
    differ: Any = None
    applier: Any = None
    health_mod: Any = None
    invariants_mod: Any = None
    binding_store_mod: Any = None
    outbound_differ_mod: Any = None
    inbound_differ_mod: Any = None
    local_label_intent_mod: Any = None
    sync_logger_mod: Any = None
    mode_mod: Any = None
    sync_logger: Any = None
    local_tickets: list = field(default_factory=list)
    binding_store: Any = None
    tracker_dir: Path | None = None
    prev_path: Path | None = None
    prev_snapshot: dict = field(default_factory=dict)
    curr_path: Path | None = None
    curr_snapshot: dict = field(default_factory=dict)
    # populated by run_differs (sibling run_differs.py)
    mutations: list = field(default_factory=list)
    # pending-binding recovery failures this pass (story 9622); tally-only, not a gate
    recovery_failures: int = 0
    # populated by _apply_mutations
    unfiltered_count: int = 0
    manifest_path: Any = None
    nowrite_plan: dict | None = None


def reconcile_once(
    pass_id: str,
    repo_root: Path | None = None,
    target_mode=None,
    filter_local_ids: set[str] | None = None,
    abort_check=None,
) -> dict:
    """Run one reconciler pass: fetch → diff → apply.

    Reads the previous snapshot (written at the end of the prior pass) from
    ``bridge_state/snapshots/<pass_id>.prev.json``, fetches the current
    remote state, computes mutations, applies them, then advances the prev
    snapshot file so the next call is idempotent against an unchanged remote.

    The pass now includes bidirectional sync:
      1. Legacy inbound path (snapshot diff → typed Mutations)
      2. Outbound path (local→Jira via outbound_differ + binding_store)
      3. New inbound path (Jira→local via inbound_differ for bound tickets)
      4. Sync logger for structured audit trail
      5. Binding store persistence at pass end

    Args:
        pass_id:       Unique identifier for this reconciliation pass.
        repo_root:     Repository root directory.  Defaults to four levels
                       above this file (rebar_reconciler/ → _engine/ → rebar/ →
                       src/ → repo root).
        filter_local_ids:
                       When set, restricts which mutations reach the applier.
                       All three differs run on their full, unfiltered inputs
                       (same code paths as production).  Only mutations whose
                       target or provenance matches a local ID in this set
                       (or its bound Jira key) are dispatched.  ``None``
                       (default) means no filtering — full reconciliation.

    Returns:
        ``{"pass_id": pass_id, "mutation_count": N, "manifest_path": str}``
        where N is the number of mutations dispatched in this pass.
    """
    if repo_root is None:
        repo_root = Path(os.environ.get("REBAR_ROOT") or Path(__file__).resolve().parents[4])
    ctx = _PassContext(
        pass_id=pass_id,
        repo_root=repo_root,
        target_mode=target_mode,
        filter_local_ids=filter_local_ids,
        abort_check=abort_check,
    )
    _load_snapshots(ctx)
    # Diff phase lives in the sibling run_differs.py (loaded lazily by file path,
    # matching the sibling-loader convention). route_inbound_probe is passed in
    # (rather than imported) so run_differs.py holds no back-edge to reconcile.py —
    # route_inbound_probe stays here because it is a separately-tested public surface.
    run_differs_mod = _load("reconcile_run_differs", "run_differs.py")
    run_differs_mod.run_differs(ctx, route_inbound_probe)
    _apply_mutations(ctx)
    return _persist_and_log(ctx)


def _load_snapshots(ctx: _PassContext) -> None:
    """Load phase: sibling modules + persist flag + sync logger + local tickets +
    binding store + the prev/curr snapshots (aborting via _handle_corrupt_snapshot
    on a corrupt prev_snapshot). Populates ctx for the diff/apply/persist phases.
    """
    pass_id = ctx.pass_id
    repo_root = ctx.repo_root
    target_mode = ctx.target_mode
    filter_local_ids = ctx.filter_local_ids
    fetcher = _load("reconcile_fetcher", "fetcher.py")
    differ = _load("reconcile_differ", "differ.py")
    applier = _load("reconcile_applier", "applier.py")
    health_mod = _load("reconcile_health", "health.py")
    invariants_mod = _load("reconcile_invariants", "invariants.py")
    binding_store_mod = _load("reconcile_binding_store", "binding_store.py")
    outbound_differ_mod = _load("reconcile_outbound_differ", "outbound_differ.py")
    inbound_differ_mod = _load("reconcile_inbound_differ", "inbound_differ.py")
    local_label_intent_mod = _load("reconcile_local_label_intent", "local_label_intent.py")
    sync_logger_mod = _load("reconcile_sync_logger", "sync_logger.py")

    # -----------------------------------------------------------------------
    # Persistence gating (ticket yaw-plait-doe).
    #
    # cap-0 modes (dry-run, reconcile-check) are documented as read-only: they
    # run the full differ COMPUTATION and PRODUCE the report, but must write
    # NOTHING to the local store. Every write point below is gated on `persist`.
    #
    # target_mode None defaults to LIVE → persists. dry-run / reconcile-check
    # → cap 0 → persist=False. bootstrap-* / live → non-zero/None cap → persist.
    # -----------------------------------------------------------------------
    mode_mod = _load("rebar_reconciler.mode", "mode.py")
    if target_mode is None:
        persist = True
    else:
        persist = mode_mod.MODE_CAPS.get(target_mode) != 0

    # -----------------------------------------------------------------------
    # Sync logger: create at pass start, close at pass end (finally block).
    # In no-write mode use a no-op logger so no sync-log-<ts>.jsonl is created.
    # -----------------------------------------------------------------------
    log_path = repo_root / "bridge_state" / f"sync-log-{pass_id}.jsonl"
    sync_logger = sync_logger_mod.SyncLogger(log_path) if persist else _NoOpSyncLogger()
    sync_logger.log(
        "sync_pass_start",
        pass_id=pass_id,
        mode=target_mode.value if target_mode else "live",
        filtered=bool(filter_local_ids),
        filter_count=len(filter_local_ids) if filter_local_ids else 0,
    )
    if filter_local_ids:
        print(  # noqa: T201
            f"FILTERED PASS: scope restricted to {len(filter_local_ids)} "
            f"local IDs — not a production reconciliation."
        )

    # Ensure snapshots directory exists
    snapshots_dir = repo_root / "bridge_state" / "snapshots"
    snapshots_dir.mkdir(parents=True, exist_ok=True)

    # -----------------------------------------------------------------------
    # Read local tickets from the ticket CLI.
    # -----------------------------------------------------------------------
    local_tickets = _read_local_tickets(repo_root, no_sync=not persist)

    # -----------------------------------------------------------------------
    # Load and recover binding store.
    # -----------------------------------------------------------------------
    binding_store = binding_store_mod.load_binding_store(repo_root)

    # Read previous snapshot from the tickets-tracker directory (persisted  # tickets-boundary-ok
    # between GHA runs via the commit-back step). The earlier approach wrote
    # prev.json to bridge_state/snapshots/ on the main-branch worktree, but
    # that filesystem is ephemeral — every GHA run starts fresh, so
    # prev_snapshot was always {} and the differ re-derived all 2050+
    # inbound_create mutations on every pass. Writing to the tracker dir  # tickets-boundary-ok
    # ensures the snapshot survives between runs because the workflow's
    # commit-back step commits everything under that directory.  # tickets-boundary-ok
    tracker_dir = repo_root / ".tickets-tracker"  # tickets-boundary-ok
    prev_dir = tracker_dir / ".bridge_state"
    prev_dir.mkdir(parents=True, exist_ok=True)
    prev_path = prev_dir / "prev_snapshot.json"
    if prev_path.exists():
        try:
            prev_snapshot: dict = json.loads(prev_path.read_text())
        except (json.JSONDecodeError, ValueError, OSError) as _exc:
            # A corrupt / conflict-marked prev_snapshot must NEVER let the pass
            # proceed with an unknown Jira state — alert + abort (see helper).
            _handle_corrupt_snapshot(pass_id, repo_root, prev_path, _exc)
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

    ctx.persist = persist
    ctx.fetcher = fetcher
    ctx.differ = differ
    ctx.applier = applier
    ctx.health_mod = health_mod
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


def _handle_corrupt_snapshot(
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
    print(  # noqa: T201
        f"ERROR: prev_snapshot.json is corrupt or contains git conflict "
        f"markers and cannot be parsed. Aborting reconcile pass "
        f"'{pass_id}' to prevent emitting mutations against unknown "
        f"Jira state. File: {prev_path}. Error: {_exc}. "
        f"Recovery: resolve the merge conflict or delete the file to "
        f"force a full re-fetch on the next pass.",
        file=sys.stderr,
    )
    try:
        _alert_store = _load(
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
        print(  # noqa: T201
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


def _apply_mutations(ctx: _PassContext) -> None:
    """Apply phase: optional filter-scope narrowing + status preflight + the single
    applier.apply dispatch (wrapped so health.record_pass fires even on failure).
    Records manifest_path / nowrite_plan / the unfiltered count back onto ctx.
    """
    mutations = ctx.mutations
    filter_local_ids = ctx.filter_local_ids
    binding_store = ctx.binding_store
    pass_id = ctx.pass_id
    repo_root = ctx.repo_root
    target_mode = ctx.target_mode
    persist = ctx.persist
    applier = ctx.applier
    health_mod = ctx.health_mod
    sync_logger = ctx.sync_logger

    # Story 21dd: the reconciler's outbound apply publishes ticket writes externally
    # (and to Jira), so fail CLOSED on a store this rebar cannot interpret BEFORE any
    # mutation. Guarded by `persist` so dry-run / cap-0 preview passes (which write
    # nothing) are excluded. The reconciler resolves the store directly, so use the
    # `.tickets-tracker` boundary here — not config.tracker_dir().
    if persist:
        from rebar._store.compat import check_store_compat

        check_store_compat(repo_root / ".tickets-tracker")  # tickets-boundary-ok — Finding 2

    # -------------------------------------------------------------------
    # Post-filter: when filter_local_ids is set, discard mutations that
    # target tickets outside the filter scope.  All three differs ran on
    # their full, unfiltered inputs (same code paths as production); only
    # the dispatch set is narrowed.
    # -------------------------------------------------------------------
    unfiltered_count = len(mutations)
    if filter_local_ids:
        target_set = _build_filter_target_set(filter_local_ids, binding_store)
        mutations = [m for m in mutations if _mutation_matches_filter(m, target_set)]
        print(  # noqa: T201
            f"filter: {unfiltered_count} mutations computed, "
            f"{len(mutations)} match filter ({len(filter_local_ids)} local IDs, "
            f"{len(target_set)} target keys)",
            file=sys.stderr,
        )
        sync_logger.log(
            "filter_applied",
            unfiltered=unfiltered_count,
            filtered=len(mutations),
            target_keys=len(target_set),
        )

    # Preflight: WARN (non-fatally) if any update mutation references a status
    # not present in config.local_to_jira_status. Runs exactly once per pass,
    # before any applier dispatch. It no longer aborts the pass (Facet 3): an
    # unmapped status flows to the applier and is recorded there as a
    # per-mutation failure rather than taking down every later mutation.
    preflight_status_mapping(mutations)

    # F8: wrap apply in try/except/finally so health.record_pass STILL fires
    # on apply failure with degraded fields (local_mutation_count=0,
    # failure_kind set). Without this wrapping, failed passes were invisible
    # to monitoring.
    #
    # Direction-aware dispatch lives inside applier.apply (PR #371 / defect
    # #8): the applier partitions typed Mutations by direction internally and
    # routes inbound via _apply_typed per-mutation, outbound via the batch
    # path. The previous reconcile_once-level typed/legacy split (commit
    # cb858e468d) was a parallel workaround for the same gap; with cap
    # enforcement landing in applier.apply (story 286b), all mutations must
    # flow through that single entry point so caps apply uniformly across
    # both directions.
    manifest_path = None
    nowrite_plan: dict | None = None
    apply_exc: BaseException | None = None
    try:
        # Backward compatibility: tests stub applier.apply with a signature
        # that does not accept the `mode` kwarg. Only pass it when caller
        # actually supplied a target_mode (i.e., when cap enforcement is
        # requested).
        # Only forward abort_check when set, so tests that stub applier.apply with
        # a narrower signature are unaffected (epic dust-troth-naval).
        _abort_kw = {"abort_check": ctx.abort_check} if ctx.abort_check is not None else {}
        if target_mode is None:
            manifest_path = applier.apply(
                mutations, pass_id, repo_root, binding_store=binding_store, **_abort_kw
            )
        else:
            manifest_path = applier.apply(
                mutations,
                pass_id,
                repo_root,
                mode=target_mode,
                binding_store=binding_store,
                persist=persist,
                **_abort_kw,
            )
    except BaseException as exc:  # noqa: BLE001 — must re-raise after recording
        apply_exc = exc
        raise
    finally:
        # In no-write mode, apply() returns the computed plan dict instead of
        # a manifest Path. Capture it for the report and treat manifest_path
        # as None so no on-disk manifest is expected by the tally below.
        if not persist and isinstance(manifest_path, dict):
            nowrite_plan = manifest_path
            manifest_path = None
        # health.record_pass writes a bridge_state/health/<ts>.json file —
        # skip it in no-write mode (ticket yaw-plait-doe).
        if persist:
            per_type_counts = health_mod.count_open_by_type(repo_root=repo_root)
            if apply_exc is None:
                health_mod.record_pass(
                    pass_id=pass_id,
                    pre_fsck=0,
                    post_fsck=0,
                    per_type_counts=per_type_counts,
                    local_mutation_count=len(mutations),
                    repo_root=repo_root,
                )
            else:
                # Classify the failure: reschedule vs generic apply error.
                failure_kind = (
                    "reschedule" if type(apply_exc).__name__ == "RescheduleError" else "apply_error"
                )
                health_mod.record_pass(
                    pass_id=pass_id,
                    pre_fsck=0,
                    post_fsck=0,
                    per_type_counts=per_type_counts,
                    local_mutation_count=0,
                    repo_root=repo_root,
                    failure_kind=failure_kind,
                )

    ctx.mutations = mutations
    ctx.unfiltered_count = unfiltered_count
    ctx.manifest_path = manifest_path
    ctx.nowrite_plan = nowrite_plan


def _advance_baselines(binding_store: Any, curr_snapshot: Mapping[str, Any]) -> int:
    """Advance every CONFIRMED binding's per-binding baseline to the current snapshot
    (story d6bd — the always-on successor to the retired dual-write shadow). Only
    confirmed bindings whose Jira key is in the current fetch window are advanced (an
    out-of-window key has no fresh value this pass); ``set_baseline`` filters to the
    mirrored fields. In-memory until the caller's ``save()`` persists them (ADR 0026).
    """
    advanced = 0
    for local_id, entry in binding_store.all_bindings().items():
        if entry.get("state") != "confirmed":
            continue
        jira_key = entry.get("jira_key")
        if not jira_key or jira_key not in curr_snapshot:
            continue
        binding_store.set_baseline(local_id, curr_snapshot[jira_key])
        advanced += 1
    return advanced


def _persist_and_log(ctx: _PassContext) -> dict:
    """Persist phase: save+commit the binding store, advance the prev snapshot
    (idempotency), tally the truthful applied/failure counts from the manifest,
    close the sync logger, and assemble the result dict.
    """
    persist = ctx.persist
    binding_store = ctx.binding_store
    repo_root = ctx.repo_root
    curr_path = ctx.curr_path
    prev_path = ctx.prev_path
    manifest_path = ctx.manifest_path
    nowrite_plan = ctx.nowrite_plan
    mutations = ctx.mutations
    pass_id = ctx.pass_id
    sync_logger = ctx.sync_logger
    target_mode = ctx.target_mode
    filter_local_ids = ctx.filter_local_ids
    unfiltered_count = ctx.unfiltered_count

    # -------------------------------------------------------------------
    # Post-apply: save binding store, advance snapshot, close sync logger.
    # -------------------------------------------------------------------
    # binding_store.save() writes .bridge_state/bindings.json; the commit
    # helper writes/commits it to the tickets branch. Both are store writes —
    # skip the entire block in no-write mode (ticket yaw-plait-doe).
    if persist:
        # Convergence rollout retired (story d6bd): ALWAYS advance the per-binding
        # baselines from the current snapshot (formerly gated on the removed
        # reconciler.baseline_dual_write). This records the last-synced Jira-side
        # ancestor the outbound field differ arbitrates against (ADR 0026). Runs
        # BEFORE save() so they persist this pass; fail-open (never break a sync pass).
        try:
            _advance_baselines(binding_store, ctx.curr_snapshot)
        except Exception as exc:  # noqa: BLE001 — baseline advance is best-effort; never break sync
            print(  # noqa: T201
                f"reconcile: baseline advance failed ({exc})",
                file=sys.stderr,
            )
        try:
            binding_store.save()
            # Commit the updated bindings.json to the tickets orphan branch so
            # it survives a concurrent ``git merge origin/tickets`` in the
            # ticket-CLI's _push_tickets_branch() between reconciler passes.
            # Without this commit, local probe runs lose newly-created bindings
            # on the next ticket-CLI push, causing the next reconciler pass to
            # see bound tickets as unbound and generate CREATE rather than
            # UPDATE mutations (regression: outbound scalar edits never land).
            if not _commit_binding_store_snapshot(binding_store, repo_root, pass_id):
                # Commit failed — bindings are on disk but NOT on the tickets
                # branch. A concurrent ``git merge origin/tickets`` between now
                # and the next pass can clobber the working-tree bindings.json
                # with the remote version, making bound tickets appear unbound
                # (cf93b2b7ad class). _commit_binding_store_snapshot already
                # logged the error and filed the alert. Do NOT abort the pass —
                # commit failure must never break sync.
                print(  # noqa: T201
                    "ERROR: reconcile: binding-store commit to tickets branch failed; "
                    "bindings are at risk of clobber on the next 'git merge origin/tickets'. "
                    "The current pass will complete normally. Check git state in "
                    ".tickets-tracker and ensure the GHA commit-back step runs to persist "
                    "bindings before the next reconciler pass.",
                    file=sys.stderr,
                )
        except Exception as exc:  # noqa: BLE001 — fail-open: save failure must never break sync, log only
            print(  # noqa: T201
                f"reconcile: binding store save failed ({exc})",
                file=sys.stderr,
            )

        # Advance prev snapshot so the next call converges to zero mutations.
        # Only in persist mode: curr_path is None in no-write mode and the
        # prev_snapshot must stay untouched (no store write). Both paths are
        # guaranteed set by _load_snapshots in persist mode (the invariant the
        # surrounding ``if persist`` encodes) — assert it to narrow the Optional.
        assert curr_path is not None and prev_path is not None
        shutil.copy2(curr_path, prev_path)

    # Bug 85a1: surface the truthful applied-count and failure-count by parsing
    # the manifest written by _apply_batch. Before this fix, sync_pass_end and
    # the result dict reported mutations_applied=len(mutations) — the COMPUTED
    # count, not the count that actually reached a handler. The "OK: converged"
    # message in __main__ inherited that lie.
    #
    # Semantics: a manifest outcome with no "error" key counts as applied (the
    # handler ran without raising — even update_one's comment-fallback path that
    # returns result=None on 400 illegal-transition counts as applied because a
    # comment was added). An outcome with an "error" key counts as a failure.
    #
    # Degrades gracefully: if manifest_path is None (rare paths), or the JSON
    # cannot be parsed, the counts conservatively default to (mutation_count, 0)
    # so existing callers reading mutations_applied receive a number consistent
    # with the prior contract.
    # No-write (cap-0) mode: nothing is applied, so the tally is (0, 0) and the
    # computed plan comes from the in-memory rendered dict (no manifest file).
    mutations_applied = len(mutations)
    mutation_failures = 0
    if nowrite_plan is not None:
        mutations_applied = 0
        mutation_failures = 0
    elif manifest_path is not None:
        try:
            manifest_data = json.loads(Path(manifest_path).read_text())
            # Two manifest shapes coexist (bug 85a1 follow-up):
            #   1. Legacy/LIVE — written by _apply_batch with a flat
            #      ``mutations`` list of outcome dicts; each outcome with no
            #      ``error`` key counts as applied.
            #   2. Asymmetric/BOOTSTRAP — written by manifest_renderer when
            #      mode caps are in effect (bootstrap-strict/throttle/dry-run).
            #      Carries an explicit ``applied_count`` integer and direction
            #      totals; no flat ``mutations`` list.
            # Detect the asymmetric shape via the presence of ``applied_count``
            # and prefer it when present (it's the authoritative apply tally).
            # Otherwise fall back to the legacy outcomes-list count.
            if "applied_count" in manifest_data:
                mutations_applied = int(manifest_data["applied_count"])
                mutation_failures = int(manifest_data.get("failed_count", 0))
            else:
                outcomes = manifest_data.get("mutations", []) or []
                mutations_applied = sum(1 for o in outcomes if not o.get("error"))
                mutation_failures = sum(1 for o in outcomes if o.get("error"))
        except Exception as exc:  # noqa: BLE001 — fail-open: fall back to computed count, log only
            print(  # noqa: T201
                f"reconcile: manifest tally read failed ({exc}) — falling back to computed count",
                file=sys.stderr,
            )

    # Story 9622: pending-binding recovery failures (set by run_differs on ctx) are
    # surfaced as a tally — observability-only, NOT an exit gate (recovery is
    # best-effort/fail-open; a transient Jira search hiccup must not fail the pass).
    recovery_failures = int(getattr(ctx, "recovery_failures", 0) or 0)

    sync_logger.log(
        "sync_pass_end",
        pass_id=pass_id,
        mutations_computed=len(mutations),
        mutations_applied=mutations_applied,
        mutation_failures=mutation_failures,
        recovery_failures=recovery_failures,
    )
    sync_logger.close()

    result = {
        "pass_id": pass_id,
        "mutation_count": len(mutations),
        "mutations_applied": mutations_applied,
        "mutation_failures": mutation_failures,
        "recovery_failures": recovery_failures,
        "manifest_path": str(manifest_path) if manifest_path is not None else None,
    }
    # No-write (cap-0) mode: surface the COMPUTED plan in the result so callers
    # (rebar.reconcile / MCP) receive the detailed mutation plan even though no
    # manifest file was written (ticket yaw-plait-doe).
    if nowrite_plan is not None:
        result["no_write"] = True
        result["mode"] = getattr(target_mode, "value", str(target_mode))
        result["plan"] = _build_plan_entries(mutations)
    if filter_local_ids:
        result["filtered"] = True
        result["filter_local_ids"] = sorted(filter_local_ids)
        result["unfiltered_mutation_count"] = unfiltered_count
    return result
