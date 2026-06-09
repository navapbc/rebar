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
from pathlib import Path
from collections.abc import Mapping
from typing import Any


# ---------------------------------------------------------------------------
# Typed-mutation dispatch table
#
# Maps (direction_value: str, action_value: str) → leaf callable from applier.
# Built lazily at first call to _dispatch_mutation so that top-level import of
# reconcile.py does NOT pull in applier (preserves the existing test invariant:
# test_import_does_not_load_fetcher / T3's import-topology guard).
#
# INVALID_PAIRS lists (direction, action) string pairs that are NOT routed by
# the dispatch table and are NOT in mutation._VALID_COMBINATIONS — pairs that
# neither direction has semantics for.  Currently empty because _VALID_COMBINATIONS
# already excludes the two inbound-only actions from the outbound direction;
# the dispatch table is built solely from _VALID_COMBINATIONS.
# ---------------------------------------------------------------------------

INVALID_PAIRS: frozenset[tuple[str, str]] = frozenset(
    {
        ("outbound", "clean_label"),
        ("outbound", "repair_property"),
    }
)
"""String (direction, action) pairs with no valid dispatch arm and no applier semantics.

These two pairs are inbound-only actions: clean_label and repair_property have no
outbound semantics (they only apply to Jira-side data corrections).  The pairs are
excluded from mutation._VALID_COMBINATIONS by _INBOUND_ONLY_ACTIONS; they are listed
here so the enumerative coverage test can verify completeness of the dispatch table
over the full {inbound, outbound} × MutationAction cartesian product.
"""

_DISPATCH_TABLE: dict[tuple[str, str], object] | None = None


def _build_dispatch_table() -> dict[tuple[str, str], object]:
    """Build and return the (direction_str, action_str) → leaf callable mapping.

    Loads the applier module via ``_load()`` (the same lazy-loader used by
    ``reconcile_once``) so that applier.py is NOT imported at reconcile
    module-load time, preserving the import topology invariant tested in
    test_reconcile_main.py.  Fetching the leaf functions via ``getattr``
    after the module load avoids relative imports, which cannot resolve when
    reconcile.py is loaded via ``importlib.util.spec_from_file_location``
    outside of a package context (as in the unit-test harness).
    """
    applier = _load("reconcile_applier", "applier.py")

    return {
        ("inbound", "create"): getattr(applier, "_apply_inbound_create"),
        ("inbound", "update"): getattr(applier, "_apply_inbound_update"),
        ("inbound", "delete"): getattr(applier, "_apply_inbound_delete"),
        ("inbound", "probe"): getattr(applier, "_apply_inbound_probe"),
        ("inbound", "clean_label"): getattr(applier, "_apply_inbound_clean_label"),
        ("inbound", "repair_property"): getattr(
            applier, "_apply_inbound_repair_property"
        ),
        ("inbound", "conflict"): getattr(applier, "_apply_inbound_conflict"),
        ("outbound", "create"): getattr(applier, "_apply_outbound_create"),
        ("outbound", "update"): getattr(applier, "_apply_outbound_update"),
        ("outbound", "delete"): getattr(applier, "_apply_outbound_delete"),
        ("outbound", "probe"): getattr(applier, "_apply_outbound_probe"),
        ("outbound", "conflict"): getattr(applier, "_apply_outbound_conflict"),
    }


def _dispatch_mutation(mutation: Any, context: Any = None) -> Any:
    """Route a typed Mutation to its leaf handler in the applier.

    Dispatches based on (mutation.direction, mutation.action) string values
    via the module-level ``_DISPATCH_TABLE``.  The table is built lazily on
    the first call to avoid pulling applier into the import graph at module
    load time.

    Args:
        mutation: A ``mutation.Mutation`` (or duck-typed object with
                  ``.direction`` and ``.action`` string-valued attributes).
        context:  Optional call context forwarded to the leaf (currently
                  unused by stub leaves; reserved for client injection).

    Returns:
        The ``ApplyResult`` returned by the matching leaf callable.

    Raises:
        NotImplementedError: When (direction, action) is not in the dispatch
            table, naming the tuple in the message so callers can identify
            the unhandled pair.
    """
    global _DISPATCH_TABLE
    if _DISPATCH_TABLE is None:
        _DISPATCH_TABLE = _build_dispatch_table()

    d = str(getattr(mutation.direction, "value", mutation.direction))
    a = str(getattr(mutation.action, "value", mutation.action))
    key = (d, a)

    leaf = _DISPATCH_TABLE.get(key)
    if leaf is None:
        raise NotImplementedError(
            f"no dispatch arm for (direction={d!r}, action={a!r})"
        )
    return leaf(mutation, client=context)


class StatusMappingError(Exception):
    """Raised when a mutation references a local status absent from
    ``config.local_to_jira_status``. The preflight scan raises this before the
    applier dispatch loop runs so an unmapped status cannot be silently
    forwarded to Jira."""


def preflight_status_mapping(mutations) -> None:
    """Raise :class:`StatusMappingError` if any update mutation references a
    status absent from ``config.local_to_jira_status``.

    An empty mapping disables the scan (kill-switch). Non-update mutations and
    mutations whose ``fields`` payload does not include a ``status`` key are
    ignored.
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
            raise StatusMappingError(
                f"local status {status!r} not in local_to_jira_status mapping "
                f"(target={target})"
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
    import subprocess as _sp

    tracker_dir = repo_root / ".tickets-tracker"  # tickets-boundary-ok
    # Bug 1e08: stage BOTH the live store and the retired-binding store. The
    # absence-lifecycle GC writes bindings-retired.json; a retirement-only pass
    # must also be committed (else a soft-deleted binding is silently lost on
    # the next ``git merge origin/tickets``).
    _rel_files = [".bridge_state/bindings.json", ".bridge_state/bindings-retired.json"]
    _existing_rel = [rel for rel in _rel_files if (tracker_dir / rel).exists()]
    if not _existing_rel:
        return True  # Nothing to commit — not a failure

    try:
        # Stage only our two state files (never git add -A: avoid staging
        # unrelated working-tree changes in the tickets worktree).
        _sp.run(
            ["git", "-C", str(tracker_dir), "add", *_existing_rel],
            check=True,
            capture_output=True,
            text=True,
        )
        # Check if there is actually a diff to commit (idempotent).
        status = _sp.run(
            ["git", "-C", str(tracker_dir), "diff", "--cached", "--name-only"],
            check=True,
            capture_output=True,
            text=True,
        )
        # PER-FILE idempotency (bug 1e08): the prior substring test
        # ``"bindings.json" not in status.stdout`` does NOT match
        # ``bindings-retired.json`` as a distinct file, so a retirement-only
        # change (only bindings-retired.json staged) would be silently skipped.
        # Match on basename membership over the staged-file lines instead.
        _staged_basenames = {
            os.path.basename(line.strip())
            for line in status.stdout.splitlines()
            if line.strip()
        }
        if not ({"bindings.json", "bindings-retired.json"} & _staged_basenames):
            return True  # Already up-to-date; nothing to commit.
        _sp.run(
            [
                "git",
                "-C",
                str(tracker_dir),
                "commit",
                "--no-verify",
                "-q",
                "-m",
                f"reconciler: persist binding-store snapshot [pass {pass_id}]",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return True
    except Exception as exc:  # noqa: BLE001
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
        except Exception as _alert_exc:  # noqa: BLE001
            print(  # noqa: T201
                f"ERROR: alert_store write also failed ({_alert_exc}); "
                f"binding-commit failure not persisted to bridge_alerts.",
                file=sys.stderr,
            )
        return False


def _load(name: str, relpath: str):
    """Load a sibling module by relative file path, registering it in sys.modules.

    Returns the cached module when ``name`` is already in ``sys.modules``;
    this allows test fixtures to pre-register patched modules and have
    ``reconcile_once`` reuse them rather than loading fresh copies.
    """
    if name in sys.modules:
        return sys.modules[name]
    path = Path(__file__).parent / relpath
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _audit_log_probe(
    branch_label: str, issue_key: str, detail: dict | None = None
) -> None:
    """Write a single audit-log entry to stderr for log-only probe branches.

    Used by :func:`route_inbound_probe` for branches that produce no follow-on
    mutation (``trash_restore`` / PRESENT_RESOLVED and ``unreachable`` /
    UNREACHABLE) so that the probe outcome is still durably observable in
    pass logs.
    """
    detail_str = "" if not detail else f" detail={detail!r}"
    print(  # noqa: T201
        f"inbound_probe: branch={branch_label} key={issue_key}{detail_str}"
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


def _read_local_tickets(repo_root: Path) -> list[dict]:
    """Read local tickets from the ticket CLI, falling back to empty list.

    In production the ticket CLI is the ``rebar`` dispatcher (``rebar list``),
    resolved from REBAR_TICKET_CLI or the engine dir alongside this package.
    If the CLI is unavailable (unit tests, minimal environments), return an
    empty list with a warning on stderr.
    """
    import os as _os  # local import to avoid top-level dep
    import subprocess as _sp  # local import to avoid top-level dep

    cli = Path(
        _os.environ.get("REBAR_TICKET_CLI")
        or (Path(__file__).resolve().parent.parent / "rebar")
    )
    if not cli.exists():
        print(  # noqa: T201
            "reconcile: ticket CLI not found — local_tickets=[]",
            file=sys.stderr,
        )
        return []
    try:
        result = _sp.run(
            [str(cli), "list"],
            capture_output=True,
            text=True,
            cwd=str(repo_root),
            timeout=60,
        )
        if result.returncode != 0:
            print(  # noqa: T201
                f"reconcile: ticket CLI exited {result.returncode} — local_tickets=[]",
                file=sys.stderr,
            )
            return []
        return json.loads(result.stdout)
    except Exception as exc:  # noqa: BLE001
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


def reconcile_once(
    pass_id: str,
    repo_root: Path | None = None,
    target_mode=None,
    filter_local_ids: set[str] | None = None,
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
                       above this file (rebar_reconciler/ → scripts/ → dso/ →
                       plugins/ → repo root).
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
        repo_root = Path(os.environ.get("REBAR_ROOT") or os.environ.get("PROJECT_ROOT") or Path(__file__).resolve().parents[4])

    fetcher = _load("reconcile_fetcher", "fetcher.py")
    differ = _load("reconcile_differ", "differ.py")
    applier = _load("reconcile_applier", "applier.py")
    health_mod = _load("reconcile_health", "health.py")
    invariants_mod = _load("reconcile_invariants", "invariants.py")
    binding_store_mod = _load("reconcile_binding_store", "binding_store.py")
    outbound_differ_mod = _load("reconcile_outbound_differ", "outbound_differ.py")
    inbound_differ_mod = _load("reconcile_inbound_differ", "inbound_differ.py")
    local_label_intent_mod = _load(
        "reconcile_local_label_intent", "local_label_intent.py"
    )
    sync_logger_mod = _load("reconcile_sync_logger", "sync_logger.py")

    # -----------------------------------------------------------------------
    # Sync logger: create at pass start, close at pass end (finally block).
    # -----------------------------------------------------------------------
    log_path = repo_root / "bridge_state" / f"sync-log-{pass_id}.jsonl"
    sync_logger = sync_logger_mod.SyncLogger(log_path)
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
    local_tickets = _read_local_tickets(repo_root)

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
                        "reason": (
                            f"prev_snapshot.json corrupt/unparseable at {prev_path}: "
                            f"{_exc}"
                        ),
                        "pass_id": pass_id,
                        "file": str(prev_path),
                        "resolved": False,
                        "timestamp_ns": __import__("time").time_ns(),
                    },
                    repo_root,
                )
            except Exception as _alert_exc:  # noqa: BLE001
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
    else:
        prev_snapshot = {}

    # Fetch current remote state
    curr_path = fetcher.fetch_snapshot(pass_id, repo_root)
    curr_snapshot: dict = json.loads(curr_path.read_text())

    # Check structural invariants on the post-fetch snapshot, before diffing.
    # check_at_most_one_dso_local_id returns only the filed violations (capped
    # at 5 per pass — see invariants._CAP_PER_PASS), so the prior log line's
    # "violations" and "filed" numbers were identical by construction. F11: log
    # filed count with the cap for clarity.
    #
    # Filtered passes skip invariant bug-filing to avoid side effects on
    # pre-existing violations outside the test scope.
    if filter_local_ids:
        print(  # noqa: T201
            f"invariants: skipped (filtered pass, {len(curr_snapshot)} issues in snapshot)"
        )
    else:
        filed = invariants_mod.check_at_most_one_dso_local_id(
            curr_snapshot, repo_root=repo_root
        )
        print(  # noqa: T201
            f"invariants: scanned={len(curr_snapshot)} filed={len(filed)} (cap=5)"
        )

    # Invariant phase: verify dual-identity round-trip on the post-fetch
    # snapshot before diffing. Quarantine one-sided keys (skipped by the
    # differ) and seed repair_property mutations for one-sided dso_local_id
    # rows so the differ emits the repair in this same pass.
    #
    # Filtered passes skip this to avoid seeding repair mutations for
    # non-test tickets that would leak outside the filter scope.
    if filter_local_ids:
        quarantine_keys: set[str] = set()
        seed_repair_property_mutations: list = []
    else:
        quarantine_keys, seed_repair_property_mutations = (
            invariants_mod.check_dual_identity_complete(prev_snapshot, curr_snapshot)
        )

    # Compute mutations (pure function, no I/O). The invariant signals are
    # passed through so the differ honors quarantine + seed mutations.
    mutations = differ.compute_mutations(
        prev_snapshot,
        curr_snapshot,
        quarantine_set=quarantine_keys,
        seed_mutations=seed_repair_property_mutations,
    )

    # Post-emit filter: scan mutations for repair_property follow-ons that
    # carry a schema_drift kind. report_schema_drift surfaces each drift via
    # stderr WARN so the signal is not swallowed.
    #
    # CONTRACT NOTE: applier.inbound_repair_property emits follow-ons with
    # kind="schema_drift_signal" (see applier.py:1142), but this loop matches
    # kind=="schema_drift". The naming mismatch means follow-ons produced
    # from the in-pass repair_property failure path are NOT picked up here.
    # The current consumers of this filter are the differ + test fixtures
    # that emit kind="schema_drift" directly. Aligning these is tracked
    # separately under meta-bug 5f2a-9a9f-2b4a-4aab.
    #
    # Mutations may be plain dicts (legacy schema) or Mutation dataclass
    # instances (canonical contract from epic 4047 / cde1). Normalise on
    # access — mirrors the same dual-shape pattern in
    # preflight_status_mapping below. Pre-fix this loop used
    # `_m.get("action")` which crashed with "'Mutation' object has no
    # attribute 'get'" once the reconciler reached this code path in
    # production with typed Mutations.
    mut_mod_for_action = _load("reconcile_mutation", "mutation.py")
    for _m in mutations:
        action_attr = getattr(_m, "action", None)
        if action_attr is not None:
            # Typed Mutation shape. Normalise enum/string for comparison.
            action_str = getattr(action_attr, "value", action_attr)
            payload = getattr(_m, "payload", None) or {}
            follow_on = (
                payload.get("follow_on") if isinstance(payload, Mapping) else None
            )
        else:
            # Legacy dict shape.
            action_str = _m.get("action")
            follow_on = _m.get("follow_on")
        if action_str != mut_mod_for_action.MutationAction.repair_property.value:
            continue
        if isinstance(follow_on, Mapping) and follow_on.get("kind") == "schema_drift":
            invariants_mod.report_schema_drift(
                follow_on.get("target"),
                follow_on.get("observed"),
                follow_on.get("expected"),
            )

    # Inbound-probe dispatch: any (inbound, probe) Mutation emitted by the
    # differ is routed through the live inbound_probe classifier, then
    # converted into a branch-specific follow-on (or a log-only outcome) via
    # route_inbound_probe. Follow-on mutations are appended in-place so the
    # applier dispatches them in the same pass.
    mut_mod = _load("reconcile_mutation", "mutation.py")
    probe_mod = _load("inbound_probe", "inbound_probe.py")
    probe_follow_ons: list = []
    for _m in mutations:
        # Only Mutation objects with the (inbound, probe) combo trigger a probe.
        direction = getattr(_m, "direction", None)
        action = getattr(_m, "action", None)
        if direction is None or action is None:
            continue
        if direction != mut_mod.MutationDirection.inbound:
            continue
        if action != mut_mod.MutationAction.probe:
            continue
        try:
            probe_result = probe_mod.probe(_m.target)
        except probe_mod.ProbeConfigError as exc:
            # Missing env → treat as unreachable; do not abort the pass.
            print(  # noqa: T201
                f"inbound_probe: skipped key={_m.target} reason=config_error err={exc}",
                file=sys.stderr,
            )
            continue
        follow_ons = route_inbound_probe(_m, probe_result)
        if follow_ons:
            probe_follow_ons.extend(follow_ons)
    if probe_follow_ons:
        mutations.extend(probe_follow_ons)

    # -------------------------------------------------------------------
    # Outbound differ: local → Jira mutations via binding store.
    #
    # Recover any pending bindings from prior failed passes, then compute
    # outbound mutations from local tickets vs. Jira snapshot. Each
    # OutboundMutation is converted to a typed Mutation so it flows through
    # the unified applier.apply() dispatch (cap enforcement, direction-aware
    # routing).
    # -------------------------------------------------------------------
    # Filtered passes skip pending-binding recovery to avoid finalizing
    # bindings for non-test tickets (scope leak).
    if not filter_local_ids:
        try:
            binding_store.recover_pending_bindings(applier)
        except Exception as exc:  # noqa: BLE001
            # Recovery failure is non-fatal — log and continue.
            print(  # noqa: T201
                f"reconcile: binding recovery failed ({exc}), continuing",
                file=sys.stderr,
            )

    # Bug a06c: compute per-binding label-intent map BEFORE the differ
    # runs. The outbound differ uses it to gate REMOVE emission so that
    # labels Jira added side-band (which local never had) do not produce
    # spurious REMOVEs — those spurious REMOVEs cancel legitimate
    # inbound ADDs under the PR #457 local-wins bidir suppression
    # contract, silently dropping the label on both sides (the T3 IB-ADD
    # probe failure). Only bound tickets need intent; unbound tickets
    # emit creates with their full tag set unconditionally.
    bound_local_ids = [
        t.get("ticket_id", t.get("id", ""))
        for t in local_tickets
        if binding_store.get_jira_key(t.get("ticket_id", t.get("id", ""))) is not None
    ]
    local_label_intent = local_label_intent_mod.compute_label_intent_map(
        bound_local_ids, tracker_dir
    )

    # Bug 4292: create an AcliClient for the outbound differ's live comment
    # fetch path. Jira search results (used by fetcher.fetch_snapshot) do NOT
    # include the comment field — so every live snapshot entry lacks "comment"
    # data. Without a client, _diff_comments would fall back to jira_comments=[]
    # and re-emit every local comment as an "add" on every pass. The client is
    # used at most once per bound ticket with local comments (bounded call count).
    # The client is created here (rather than inside outbound_differ.py) so the
    # differ stays importable in test environments without JIRA_URL/JIRA_USER
    # env vars set, and to keep the I/O-free fixture path intact.
    # Use "acli_integration" as the sys.modules key — same canonical key used
    # by applier._load_acli() so the module is shared and not double-loaded.
    acli_mod_for_comments = _load("acli_integration", "../acli-integration.py")
    outbound_diff_client = acli_mod_for_comments.AcliClient(
        jira_url=os.environ.get("JIRA_URL", ""),
        user=os.environ.get("JIRA_USER", ""),
        api_token=os.environ.get("JIRA_API_TOKEN", ""),
    )

    # Bug 0702-3b6d-c1db-4ed3 (inbound counterpart to 1e08): collect the
    # bound-but-absent ALIVE direct-GET results so the inbound differ can mirror
    # Jira-side changes for out-of-window keys WITHOUT a second GET. The
    # outbound differ records each alive (HTTP 200) absent key's raw fields
    # here; we merge them into the inbound snapshot below. 404/transport keys
    # are intentionally absent from this dict (retirement stays outbound-owned).
    absent_alive_fields: dict[str, dict] = {}
    outbound_raw = outbound_differ_mod.compute_outbound_mutations(
        local_tickets,
        curr_snapshot,
        binding_store,
        excluded_statuses={"archived", "deleted"},
        local_label_intent=local_label_intent,
        client=outbound_diff_client,
        pass_id=pass_id,
        absent_alive_fields=absent_alive_fields,
    )
    sync_logger.log(
        "outbound_differ_complete",
        count=len(outbound_raw),
    )
    # Bug b859 (Part 0c): structured per-direction breakdown to stderr so
    # operators / probes can see per-action counts without parsing the
    # sync_logger JSON manifest. Format: ``RECON: <kind> <field>=<value>``
    # with a stable token prefix that's distinct from FILTERED/filter/OK/
    # ERROR so the probe's grep filter does not need to be updated.
    _ob_creates = sum(1 for m in outbound_raw if m.action == "create")
    _ob_updates = sum(1 for m in outbound_raw if m.action == "update")
    _ob_deletes = sum(1 for m in outbound_raw if m.action == "delete")
    print(  # noqa: T201
        f"RECON: outbound_differ total={len(outbound_raw)} "
        f"create={_ob_creates} update={_ob_updates} delete={_ob_deletes}",
        file=sys.stderr,
    )

    # Convert OutboundMutation → typed Mutation for unified dispatch.
    for om in outbound_raw:
        if om.action == "create":
            typed = mut_mod.Mutation(
                direction=mut_mod.MutationDirection.outbound,
                action=mut_mod.MutationAction.create,
                target=om.local_id,
                payload={
                    **om.fields,
                    "comments": om.comments,
                    "labels": om.labels,
                    "local_id": om.local_id,
                },
                provenance={"source": "outbound_differ", "local_id": om.local_id},
            )
        elif om.action == "update":
            typed = mut_mod.Mutation(
                direction=mut_mod.MutationDirection.outbound,
                action=mut_mod.MutationAction.update,
                target=om.jira_key or om.local_id,
                payload={
                    "changed_fields": om.fields,
                    "comments": om.comments,
                    "labels": om.labels,
                },
                provenance={
                    "source": "outbound_differ",
                    "local_id": om.local_id,
                    "jira_key": om.jira_key,
                },
            )
        elif om.action == "delete":
            typed = mut_mod.Mutation(
                direction=mut_mod.MutationDirection.outbound,
                action=mut_mod.MutationAction.delete,
                target=om.jira_key or om.local_id,
                payload={},
                provenance={
                    "source": "outbound_differ",
                    "local_id": om.local_id,
                    "jira_key": om.jira_key,
                },
            )
        else:
            continue  # unknown action — skip
        mutations.append(typed)

    # -------------------------------------------------------------------
    # Inbound differ (binding-aware): Jira → local for bound tickets.
    #
    # This coexists with the legacy snapshot-diff inbound path above. The
    # legacy path handles unbound Jira issues (create/delete/probe); this
    # path handles field-level updates for already-bound tickets.
    # -------------------------------------------------------------------
    local_by_id = {t.get("ticket_id", t.get("id", "")): t for t in local_tickets}
    # Bug 3bf8: pass outbound mutations so the inbound differ can suppress
    # emissions that would contradict (and clobber) a just-emitted outbound
    # change for the same target in the same bidirectional pass.
    # Bug 3bf8: ``compute_inbound_mutations`` returns
    # ``(mutations, suppression_count)`` — the count of inbound field/label
    # items dropped by bidirectional outbound-context filtering. Single-pass
    # to avoid O(2n) differ cost on every reconcile pass.
    # Bug 0702: merge the outbound differ's alive bound-but-absent GET results
    # into the snapshot the inbound differ sees, so out-of-window Jira issues
    # are mirrored Jira→local with the GET shared across both directions (no
    # double-GET). curr_snapshot is left unmutated (shallow copy) so downstream
    # snapshot persistence is unaffected.
    inbound_snapshot = curr_snapshot
    if absent_alive_fields:
        inbound_snapshot = dict(curr_snapshot)
        inbound_snapshot.update(absent_alive_fields)
    inbound_new, _ib_suppressed = inbound_differ_mod.compute_inbound_mutations(
        inbound_snapshot,
        binding_store,
        local_by_id,
        outbound_mutations=outbound_raw,
    )
    sync_logger.log(
        "inbound_differ_complete",
        count=len(inbound_new),
    )
    _ib_with_fields = sum(1 for m in inbound_new if m.fields)
    _ib_with_labels = sum(1 for m in inbound_new if m.labels)
    _ib_with_comments = sum(1 for m in inbound_new if getattr(m, "comments", []))
    print(  # noqa: T201
        f"RECON: inbound_differ total={len(inbound_new)} "
        f"with_fields={_ib_with_fields} with_labels={_ib_with_labels} "
        f"with_comments={_ib_with_comments}",
        file=sys.stderr,
    )
    print(  # noqa: T201
        f"RECON: bidir_suppressed inbound={_ib_suppressed}",
        file=sys.stderr,
    )

    # Convert InboundMutation → typed Mutation for unified dispatch.
    for im in inbound_new:
        typed = mut_mod.Mutation(
            direction=mut_mod.MutationDirection.inbound,
            action=mut_mod.MutationAction.update,
            target=im.jira_key,
            payload={
                "local_id": im.local_id,
                # Bug b859 (Part 1a, H3 fix): _apply_inbound_update reads
                # ``fields = payload.get("fields") or payload`` at
                # applier.py:625. Writing the inbound mutation's fields
                # under "changed_fields" (mirroring the outbound convention)
                # caused the .get("fields") lookup to miss → fallback to
                # the wrapper dict whose keys never matched the
                # summary/status/etc. branches → every inbound EDIT/STATUS
                # event was silently dropped → Phase 6 idempotency churn.
                # Use "fields" so the existing applier consumer finds the
                # dict on the first lookup.
                "fields": im.fields,
                "labels": im.labels,
                # Bug 85a1 (Gap 1): propagate inbound comments so each new
                # Jira comment is written as a local COMMENT event with
                # jira_comment_id binding. getattr default keeps backward
                # compat with InboundMutation variants (legacy test stubs)
                # that lack the field.
                "comments": getattr(im, "comments", []),
            },
            provenance={
                "source": "inbound_differ",
                "jira_key": im.jira_key,
                "local_id": im.local_id,
            },
        )
        mutations.append(typed)

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
            f"{len(target_set)} target keys)"
        )
        sync_logger.log(
            "filter_applied",
            unfiltered=unfiltered_count,
            filtered=len(mutations),
            target_keys=len(target_set),
        )

    # Preflight: abort the pass if any update mutation references a status
    # not present in config.local_to_jira_status. Runs exactly once per pass,
    # before any applier dispatch, so unmapped statuses cannot reach Jira.
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
    # both directions. `_dispatch_mutation` is preserved as a public seam
    # for tests/test_dispatch_coverage.py — it is no longer called from
    # reconcile_once.
    manifest_path = None
    apply_exc: BaseException | None = None
    try:
        # Backward compatibility: tests stub applier.apply with a signature
        # that does not accept the `mode` kwarg. Only pass it when caller
        # actually supplied a target_mode (i.e., when cap enforcement is
        # requested).
        if target_mode is None:
            manifest_path = applier.apply(
                mutations, pass_id, repo_root, binding_store=binding_store
            )
        else:
            manifest_path = applier.apply(
                mutations,
                pass_id,
                repo_root,
                mode=target_mode,
                binding_store=binding_store,
            )
    except BaseException as exc:  # noqa: BLE001 — must re-raise after recording
        apply_exc = exc
        raise
    finally:
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
                "reschedule"
                if type(apply_exc).__name__ == "RescheduleError"
                else "apply_error"
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

    # -------------------------------------------------------------------
    # Post-apply: save binding store, advance snapshot, close sync logger.
    # -------------------------------------------------------------------
    try:
        binding_store.save()
        # Commit the updated bindings.json to the tickets orphan branch so
        # it survives a concurrent ``git merge origin/tickets`` in the
        # ticket-CLI's _push_tickets_branch() between reconciler passes.
        # Without this commit, local probe runs lose newly-created bindings on
        # the next ticket-CLI push, causing the next reconciler pass to see
        # bound tickets as unbound and generate CREATE rather than UPDATE
        # mutations (regression: outbound scalar-field edits never land).
        if not _commit_binding_store_snapshot(binding_store, repo_root, pass_id):
            # Commit failed — bindings are on disk but NOT on the tickets branch.
            # A concurrent ``git merge origin/tickets`` between now and the next
            # pass can clobber the working-tree bindings.json with the remote
            # version, making bound tickets appear unbound (cf93b2b7ad class).
            # _commit_binding_store_snapshot already logged the error and filed
            # the alert. Do NOT abort the pass — commit failure must never break
            # sync.
            print(  # noqa: T201
                "ERROR: reconcile: binding-store commit to tickets branch failed; "
                "bindings are at risk of clobber on the next 'git merge origin/tickets'. "
                "The current pass will complete normally. Check git state in "
                ".tickets-tracker and ensure the GHA commit-back step runs to persist "
                "bindings before the next reconciler pass.",
                file=sys.stderr,
            )
    except Exception as exc:  # noqa: BLE001
        print(  # noqa: T201
            f"reconcile: binding store save failed ({exc})",
            file=sys.stderr,
        )

    # Advance prev snapshot so the next call converges to zero mutations
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
    mutations_applied = len(mutations)
    mutation_failures = 0
    if manifest_path is not None:
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
        except Exception as exc:  # noqa: BLE001
            print(  # noqa: T201
                f"reconcile: manifest tally read failed ({exc}) — "
                f"falling back to computed count",
                file=sys.stderr,
            )

    sync_logger.log(
        "sync_pass_end",
        pass_id=pass_id,
        mutations_computed=len(mutations),
        mutations_applied=mutations_applied,
        mutation_failures=mutation_failures,
    )
    sync_logger.close()

    result = {
        "pass_id": pass_id,
        "mutation_count": len(mutations),
        "mutations_applied": mutations_applied,
        "mutation_failures": mutation_failures,
        "manifest_path": str(manifest_path),
    }
    if filter_local_ids:
        result["filtered"] = True
        result["filter_local_ids"] = sorted(filter_local_ids)
        result["unfiltered_mutation_count"] = unfiltered_count
    return result
