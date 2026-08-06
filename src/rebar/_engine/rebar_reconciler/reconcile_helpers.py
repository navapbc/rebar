#!/usr/bin/env python3
"""reconcile_helpers.py — pass-support utilities extracted from reconcile.py.

These are the leaf helpers that a reconcile pass leans on but which carry no
back-edge to the ``reconcile_once`` spine: the status-preflight scan and its
``StatusMappingError`` marker, the binding-store commit-back, the inbound-probe
router (+ its audit-log helper), the ticket-CLI reader, the filter-scope set
builders, the no-write plan renderer, and the ``_NoOpSyncLogger`` cap-0 stand-in.

Loader convention: like every sibling in this package (and mirrored by
reconcile.py / run_differs.py), this module loads its own siblings (``config.py``,
``alert_store.py``, ``inbound_probe.py``, ``mutation.py``) by file path via the
local ``_load`` helper (``importlib.util.spec_from_file_location``), so it resolves
both under the real package and when a single module is loaded standalone in tests.
It imports NOTHING from reconcile.py; reconcile.py loads this module once and
re-exports these names for attribute-access and back-compat.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

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
        _loader_spec.loader.exec_module(_loader_mod)
    lazy_load = sys.modules[_loader_key].lazy_load


def _load(name: str, relpath: str):
    """Load a sibling module by relative file path, registering it in sys.modules.

    Returns the cached module when ``name`` is already in ``sys.modules``;
    this allows test fixtures to pre-register patched modules and have
    ``reconcile_once`` reuse them rather than loading fresh copies. Delegates to
    the shared ``lazy_load`` helper (the package-wide by-path loader).
    """
    return lazy_load(name, relpath)


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
            print(
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
    # Stage the live, retired, and GET-rotation binding state files. The
    # absence-lifecycle GC writes bindings-retired.json; a retirement-only pass
    # must also be committed (else a soft-deleted binding is silently lost on
    # the next ``git merge origin/tickets``).
    _rel_files = [
        git_adapter.BINDINGS_FILE,
        git_adapter.BINDINGS_RETIRED_FILE,
        git_adapter.GET_ROTATION_FILE,
    ]
    _existing_rel = [rel for rel in _rel_files if (tracker_dir / rel).exists()]
    if not _existing_rel:
        return True  # Nothing to commit — not a failure

    try:
        # Stage only our three state files (never git add -A: avoid staging
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
            os.path.basename(git_adapter.GET_ROTATION_FILE),
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
        print(
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
            print(
                f"ERROR: alert_store write also failed ({_alert_exc}); "
                f"binding-commit failure not persisted to bridge_alerts.",
                file=sys.stderr,
            )
        return False


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
        print(
            "reconcile: ticket CLI not found — local_tickets=[]",
            file=sys.stderr,
        )
        return []
    # Build an explicit env so the live path (no_sync=False) does NOT inherit an
    # ambient REBAR_SYNC_PULL=off from the caller — that would silently suppress
    # the tickets-branch sync pull even though a syncing read was requested.
    _env = dict(_os.environ)
    if no_sync:
        _env["REBAR_SYNC_PULL"] = "off"
    else:
        _env.pop("REBAR_SYNC_PULL", None)
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
            print(
                f"reconcile: ticket CLI exited {result.returncode} — local_tickets=[]",
                file=sys.stderr,
            )
            return []
        return json.loads(result.stdout)
    except Exception as exc:  # noqa: BLE001 — fail-open: log and return empty local_tickets list
        print(
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

    def log(self, *args, **kwargs) -> None:
        return None

    def close(self) -> None:
        return None


# The differ emissions that are only sound when ``local_state`` really is local state:
# ``(outbound, update)``/``field_drift`` (bug 727f) and ``(outbound, create)``/
# ``unbound_local`` (bug d103-c3f8-2fbc-4c97). Keyed on BOTH ``source`` and ``reason`` so
# an invariant SEED mutation that happens to reuse a reason string is never swept up.
_SNAPSHOT_DIFFER_LOCAL_STATE_EMISSIONS: frozenset[tuple[str, str, str]] = frozenset(
    {
        ("outbound", "update", "field_drift"),
        ("outbound", "create", "unbound_local"),
    }
)


def drop_snapshot_differ_local_state_emissions(mutations: list[Any]) -> list[Any]:
    """Discard the differ emissions that presume a real ``local_state`` argument.

    Bugs 727f-b351-59ba-4b3b and d103-c3f8-2fbc-4c97 — one cause, two symptoms.

    ``differ.compute_mutations`` documents its arguments as ``(local_state, jira_state)``
    — the LOCAL source of truth against the Jira working set — and says so precisely
    because that contract REPLACED the legacy ``(prev_snapshot, next_snapshot)`` one.
    ``run_differs`` was never migrated: it still passes the legacy pair, both halves of
    which are REMOTE Jira state (``prev_snapshot`` is a persisted earlier fetch,
    ``curr_snapshot`` a fresh one). At that one call site ``local_state`` is therefore not
    local state at all, and the differ's two local-state-dependent arms misfire:

    * **``field_drift``** (key in both) — ``_compute_mutations_emit_both`` reads every
      prev->curr REMOTE field change as local-wins drift and emits an
      ``(outbound, update)`` carrying the STALE prev value. It never converges:
      ``reconcile.py`` advances the prev snapshot from the PRE-APPLY fetch, so an outbound
      write applied during pass N is invisible to ``prev`` at pass N+1 — a fully converged
      pair is re-planned as outbound work, and a read-only pass (which never advances
      ``prev``) re-plans the same phantom forever. Applying it changes nothing either: the
      payload is a bare field dict, not ``{"changed_fields": ...}``, so
      ``batch_dispatch._mutation_to_batch_dict`` resolves its fields to ``{}``. It is
      unsatisfiable while still spending the per-mode mutation cap and inflating
      ``mutation_count``.
    * **``unbound_local``** (key in ``local_state`` only) —
      ``_compute_mutations_emit_local_only`` emits an ``(outbound, create)`` for a key
      that is really just "present in the previous fetch, absent from this one". That is
      indistinguishable from a key that merely aged out of the working-set query, and the
      create RESURRECTS the issue from stale prev fields. It also violates ADR 0028
      (``docs/adr/0028-reconciler-bound-but-absent-not-deleted.md``, Decision para 1): no
      destructive or terminal action may be driven by a key's absence from the fetched
      snapshot.

    The differ's third arm — key in ``jira_state`` only, ``(inbound, create)`` with reason
    ``jira_new`` — IS correct here: at this call site it means a genuinely new remote
    issue, which is the snapshot diff's one real job. It is preserved, as is every other
    differ emission (inbound conflict/probe, ``dangling_jira_local_id``,
    ``duplicate_local_id``, ``ambiguous_local_binding``, ``repair_property`` follow-ons,
    the absent-partner probes). Nothing is lost by the two suppressions: field sync and
    creation for these keys are already owned in BOTH directions by the binding-aware
    outbound and inbound differ phases that run immediately afterwards.

    Scoped by ``(direction, action, provenance["source"], provenance["reason"])`` — the
    ``source`` check matters: invariant SEED mutations are prepended by
    ``compute_mutations``'s ``seed_mutations`` argument and carry
    ``provenance["source"] == "invariants"``, and a seed may legitimately reuse one of
    these reason strings. Keying on ``reason`` alone would drop it. Mutations that are
    plain dicts (legacy shape) have no ``provenance`` attribute and pass through untouched.
    Pure: returns a NEW list, never mutates in place.

    ``differ.compute_mutations`` itself is deliberately NOT modified — its documented
    local-vs-jira contract stays intact for callers that honour it, and the suppression
    lives at the one call site that does not.

    REMOVAL NOTE: if ``run_differs`` is ever migrated to pass REAL local state, this
    suppression must be DELETED in the same change — at that point both emissions become
    correct and dropping them would silently disable outbound create and outbound field
    sync.
    """
    kept: list[Any] = []
    for mutation in mutations:
        provenance = getattr(mutation, "provenance", None)
        if not isinstance(provenance, Mapping):
            kept.append(mutation)
            continue
        signature = (
            str(getattr(getattr(mutation, "direction", None), "value", "")),
            str(getattr(getattr(mutation, "action", None), "value", "")),
            str(provenance.get("reason") or ""),
        )
        if provenance.get("source") == "differ" and signature in (
            _SNAPSHOT_DIFFER_LOCAL_STATE_EMISSIONS
        ):
            continue
        kept.append(mutation)
    return kept
