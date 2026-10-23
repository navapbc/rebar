#!/usr/bin/env python3
"""Per-pass status checks, binding commits, ticket reads, and selection helpers.

These leaf helpers have no import back-edge to the reconcile spine. Sibling
loading remains patchable for standalone tests.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def _load(_name: str, relpath: str):
    """Import a sibling through the patchable package loader seam."""
    return importlib.import_module(
        "rebar.reconciler." + relpath.removesuffix(".py").replace("/", ".")
    )


class StatusMappingError(Exception):
    """Legacy marker for a status absent from both local and Jira vocabularies.

    Preflight now warns and leaves per-mutation failure handling to the applier.
    """


def _status_preflight_tolerated(mapping: Mapping[str, str], repo_root: Path | None) -> set[str]:
    """Accept built-in local/Jira status names plus configured Jira names.

    Per-project remaps may use names absent from built-ins. Config-load failure
    falls back to the built-in set without aborting the pass.
    """
    tolerated = set(mapping) | set(mapping.values())
    try:
        from rebar.reconciler import mapping_config as _mc

        tolerated |= _mc.declared_status_names(_mc.load_mapping_config(repo_root))
    except Exception:  # noqa: BLE001 — fail-open: config-declared widening is best-effort, never break the pass
        pass
    return tolerated


def preflight_status_mapping(mutations, repo_root: Path | None = None) -> None:
    """Warn on unmapped outbound update statuses without aborting the pass.

    Local keys, Jira values, and configured names are tolerated. Empty built-in
    mapping disables the scan; inbound and non-update mutations are skipped.
    The applier records each offending mutation as a failure.
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
        # Inbound statuses use Jira shape; this outbound preflight skips them.
        if direction == "inbound":
            continue
        if not isinstance(fields, dict):
            continue
        raw_status = fields.get("status")
        # Normalize REST dicts to their name before membership lookup.
        if isinstance(raw_status, dict):
            status = raw_status.get("name") or ""
        else:
            status = raw_status
        # Outbound values may still be local names or already mapped Jira
        # names; the tolerated set includes both and configured remaps.
        if status and status not in tolerated:
            # Warn on this unmapped value and let the applier record a
            # per-mutation failure; one bad status does not abort the pass.
            print(  # noqa: T201
                f"reconcile: preflight — status {status!r} is not mapped in "
                f"local_to_jira_status (neither a local-status key nor a Jira "
                f"workflow status value; target={target}); NOT aborting the pass "
                f"— the applier will record it as a per-mutation failure",
                file=sys.stderr,
            )


# Commit only 5 binding-state files through the locked store pathspec;
# broad staging would advance tickets HEAD on every pass.
def _commit_binding_store_snapshot(
    _binding_store: Any,
    repo_root: Path,
    pass_id: str,
) -> bool:
    """Commit only binding-state files through the locked tickets-branch seam.

    This protects saved bindings from a later tickets-branch merge without
    staging unrelated pass artifacts or advancing HEAD on an idempotent pass.
    Return ``False`` and report failure when the filesystem copy could not be
    committed; the reconciliation pass continues.
    """
    from rebar.config import tracker_dir as _resolve_store

    git_adapter = _load("rebar.reconciler.git_adapter", "git_adapter.py")
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
        # Scoped staging commits any changed binding-state file, including a
        # retirement-only update, and leaves HEAD still when none changed.
        # Strict seam errors become the fail-open False/alert result below.
        from rebar._store.push import commit_tickets_branch

        commit_tickets_branch(
            tracker_dir,
            message=f"reconciler: persist binding-store snapshot [pass {pass_id}]",
            paths=_existing_rel,
            strict=True,
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
                "rebar.reconciler.alert_store",
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


def _read_local_tickets(repo_root: Path, *, no_sync: bool = False) -> list[dict]:
    """Read full ticket bodies through the CLI, or warn and return empty.

    ``no_sync`` disables the tickets-branch pull for a no-write pass; ordinary
    reads explicitly clear that override so they still synchronize.
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
    """Include selected local IDs and bound Jira keys for mutation filtering."""
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
