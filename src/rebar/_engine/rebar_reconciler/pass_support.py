#!/usr/bin/env python3
"""pass_support.py — per-pass leaf helpers extracted from reconcile_helpers.py.

Moved out (ticket piscine-bullish-cowbird, module-size headroom) to keep
reconcile_helpers.py under the 800-line cap: the status-preflight scan and its
``StatusMappingError`` marker, the binding-store commit-back, the ticket-CLI
reader, the selection-resolution cluster, and the filter-scope set builders.
Every name here carries no back-edge to the ``reconcile_once`` spine — same
contract reconcile_helpers.py documents — and reconcile.py re-binds these at
module level exactly as it did when they lived in reconcile_helpers.py.

Loader convention: like every sibling in this package, this module loads its own
siblings (``config.py``, ``alert_store.py``, ``binding_store.py``) by file path via
the local ``_load`` helper, so it resolves both under the real package and when
loaded standalone in tests. It imports NOTHING from reconcile.py/reconcile_helpers.py.
"""

from __future__ import annotations

import importlib.util
import json
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
    callers reuse them rather than loading fresh copies. Delegates to
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


def _status_preflight_tolerated(mapping: Mapping[str, str], repo_root: Path | None) -> set[str]:
    """The status names the preflight tolerates: the built-in ``local_to_jira_status``
    keys (local names) UNION its values (built-in Jira names) UNION every Jira status
    NAME the mapping config for ``repo_root`` declares.

    The config-declared widening (ticket 438a) is what keeps a per-project REMAP from
    tripping a spurious warning: the mapper emits a config-only Jira name that is absent
    from the built-in keys AND values, so without this union the post-mapper preflight
    would false-alarm on exactly the config it is meant to support. Best-effort: a
    config-load failure falls back to the built-in set and never breaks the pass."""
    tolerated = set(mapping) | set(mapping.values())
    try:
        from rebar_reconciler import mapping_config as _mc

        tolerated |= _mc.declared_status_names(_mc.load_mapping_config(repo_root))
    except Exception:  # noqa: BLE001 — fail-open: config-declared widening is best-effort, never break the pass
        pass
    return tolerated


def preflight_status_mapping(mutations, repo_root: Path | None = None) -> None:
    """Scan update mutations for statuses absent from the EFFECTIVE status vocabulary
    (built-in ``config.local_to_jira_status`` keys/values UNION the config-declared Jira
    names for ``repo_root``) and WARN (non-fatally) on each.

    Facet 3 (reconciler-abort-isolation): this scan used to RAISE
    :class:`StatusMappingError` on the first unmapped status, aborting the whole
    pass before any mutation was applied. It now logs a warning to stderr for
    each offending mutation and returns normally, so the mutation flows to the
    applier and is recorded there as a per-mutation failure (fail-loud) instead
    of taking down the entire pass. :class:`StatusMappingError` remains defined
    for other references.

    An empty built-in mapping disables the scan (kill-switch). Non-update mutations,
    inbound mutations, and mutations whose ``fields`` payload does not include a
    ``status`` key are ignored. ``repo_root`` scopes the config-declared widening so a
    per-project status remap is tolerated rather than flagged (ticket 438a).
    """
    cfg = _load("reconcile_config", "config.py")
    mapping = getattr(cfg, "local_to_jira_status", {}) or {}
    if not mapping:
        return  # kill-switch — empty mapping disables preflight
    tolerated = _status_preflight_tolerated(mapping, repo_root)
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
        # Accept either by checking presence in the effective tolerated set
        # (built-in local keys ∪ built-in Jira values ∪ config-declared Jira
        # names). The preflight purpose is to catch *unmapped* statuses before
        # the applier dispatch; a per-project remap onto a config-declared name
        # is legitimately-mapped (ticket 438a).
        if status and status not in tolerated:
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


# The selective 5-file staging goes through the shared locked store seam:
# push.commit_tickets_branch takes a pathspec (ticket 11a9-b11b-e93d-4832), so the
# `add -A` sweep that broke reconcile idempotency in ticket 6454-d06e-7361-4e3d
# (staging .bridge_state/last-pass.json advanced the tickets HEAD every pass) no
# longer forces a raw git_adapter.add+commit composition here.
def _commit_binding_store_snapshot(
    _binding_store: Any,
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

    Fix: after every successful binding_store.save(), commit the file set to the
    tickets orphan branch inside the .tickets-tracker worktree, through the store's
    locked commit seam (``rebar._store.push.commit_tickets_branch``) with a pathspec
    scoped to exactly the binding-state files — never ``git add -A``, which would
    sweep in ``.bridge_state/last-pass.json`` and advance the tickets HEAD on every
    idempotent pass (ticket 6454-d06e). No push: local probe runs that don't go
    through GHA still get durable bindings, and delivery stays with the GHA
    commit-back / the store's own push paths.

    Returns:
        True  — commit succeeded (or nothing to commit — bindings already current).
        False — a subprocess error occurred; bindings persisted to filesystem only.

    Degrades gracefully: any subprocess error (git not available, tickets branch not
    checked out, no bindings path, etc.) is caught and logged to stderr; the reconciler
    pass continues and the next GHA commit-back will persist the bindings as normal. The
    caller must NOT abort on False — commit failure must never break the sync pass.
    """
    from rebar.config import tracker_dir as _resolve_store

    git_adapter = _load("rebar_reconciler.git_adapter", "git_adapter.py")
    # RESOLVED, not composed (the store is RELOCATABLE): git_adapter.TRACKER_DIR stays the
    # committed-TREE label used for the pathspec below, but the ON-DISK root is resolved.
    tracker_dir = _resolve_store(repo_root)
    # Stage the live, retired, and GET-rotation binding state files. The
    # absence-lifecycle GC writes bindings-retired.json; a retirement-only pass
    # must also be committed (else a soft-deleted binding is silently lost on
    # the next ``git merge origin/tickets``).
    _rel_files = [
        git_adapter.BINDINGS_FILE,
        git_adapter.BINDINGS_RETIRED_FILE,
        git_adapter.GET_ROTATION_FILE,
        # Bug b8b1: the impossible-inbound-link record. Every pass runs in a fresh
        # checkout, so an uncommitted record is discarded between passes and the
        # skip never takes effect in production.
        git_adapter.IMPOSSIBLE_LINKS_FILE,
        # Epic a4bd: the per-link peer-confirmation record. Same fresh-checkout
        # argument as the impossible-link record above.
        git_adapter.PEER_CONFIRMATIONS_FILE,
    ]
    _existing_rel = [rel for rel in _rel_files if (tracker_dir / rel).exists()]
    if not _existing_rel:
        return True  # Nothing to commit — not a failure

    try:
        # Commit through the shared write-locked seam, staging ONLY our state files
        # (never git add -A: avoid staging unrelated working-tree changes in the
        # tickets worktree). The pathspec-scoped status inside the seam gives the
        # PER-FILE idempotency of bug 1e08: a change in ANY of the five files —
        # including a retirement-only bindings-retired.json change — commits, and no
        # change in any of them leaves HEAD unmoved. strict=True turns every seam
        # failure (stage, commit, lock timeout, rebase guard) into a raised
        # PushDeliveryError, which the fail-open handler below converts to the
        # existing False + stderr + bridge_alerts contract.
        from rebar._store.push import commit_tickets_branch

        commit_tickets_branch(
            tracker_dir,
            message=f"reconciler: persist binding-store snapshot [pass {pass_id}]",
            paths=_existing_rel,
            strict=True,
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
    Cap-0 dry-run/preview passes set this so a no-write
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
            check=False,
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


class SelectionError(ValueError):
    """A canonical selection could not be resolved without side effects."""


class SelectionStaleError(SelectionError):
    """A preflight-resolved ticket vanished before the locked pass load."""


def _local_ticket_id(ticket: dict) -> str:
    return str(ticket.get("ticket_id") or ticket.get("id") or "")


def _local_ticket_ids(local_tickets: list[dict]) -> set[str]:
    return {local_id for ticket in local_tickets if (local_id := _local_ticket_id(ticket))}


def resolve_selection(repo_root: Path, tokens: tuple[str, ...]) -> set[str]:
    """Resolve every local ID/Jira key read-only before pass-lock inspection."""
    local_ids = _local_ticket_ids(_read_local_tickets(repo_root, no_sync=True))
    binding_store_mod = _load("reconcile_selection_binding_store", "binding_store.py")
    bindings = binding_store_mod.load_binding_store(repo_root)
    resolved: set[str] = set()
    unresolved: list[str] = []
    for token in tokens:
        local_id = token if token in local_ids else bindings.get_local_id(token)
        if local_id is None or local_id not in local_ids:
            unresolved.append(token)
        else:
            resolved.add(local_id)
    if unresolved:
        raise SelectionError(
            "unresolved --only/--except identifier(s): " + ", ".join(sorted(unresolved))
        )
    return resolved


def ensure_selection_current(selection_ids: set[str], local_tickets: list[dict]) -> None:
    """Fail closed when a preflight target vanished before the locked load."""
    missing = selection_ids - _local_ticket_ids(local_tickets)
    if missing:
        raise SelectionStaleError(
            "selection became stale before reconciliation: " + ", ".join(sorted(missing))
        )


def narrow_selection_inputs(
    kind: str,
    selection_ids: set[str],
    local_tickets: list[dict],
    prev_snapshot: dict,
    curr_snapshot: dict,
    binding_store: Any,
) -> tuple[list[dict], dict, dict]:
    """Narrow all differ inputs using the stable preflight-resolved local IDs."""
    jira_keys = {key for local_id in selection_ids if (key := binding_store.get_jira_key(local_id))}
    keep = kind == "only"
    local = [
        ticket for ticket in local_tickets if ((_local_ticket_id(ticket) in selection_ids) == keep)
    ]
    prev = {key: value for key, value in prev_snapshot.items() if ((key in jira_keys) == keep)}
    curr = {key: value for key, value in curr_snapshot.items() if ((key in jira_keys) == keep)}
    return local, prev, curr


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
