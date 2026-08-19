"""Supported repair for orphaned reverse bindings (``bridge fsck --repair``).

``.bridge_state/bindings.json`` carries two indexes that must stay in lock-step:
forward ``bindings[local_id] = {jira_key, …}`` and reverse
``reverse[jira_key] = local_id``. A reverse entry that outlives its forward entry
is reported by ``rebar bridge fsck`` as ``store_integrity`` / kind
``reverse_missing_forward`` forever, and before this verb existed nothing could
remove it — the binding-drift canary then alerts indefinitely on a benign fault,
masking any genuine future integrity problem behind a constant non-zero count.
Thirteen such keys (REB-410..REB-422) had to be repaired by reaching into
``BindingStore._data`` because no supported surface could express it (874a).

Design constraints this module honours:

* **The repair consumes the detector.** The orphan set comes from
  ``bridge_fsck.audit_store_integrity()``, never from a second copy of the rule
  — a re-derivation could drift and delete something the report never named.
  This mirrors ``tracker_maintenance``'s delegation to ``fsck``.
* **The audit stays audit-only.** ``bridge_fsck`` keeps its L9 read-only
  boundary; only ``main()`` dispatches here.
* **Deletion goes through the public API.** ``BindingStore.unbind()`` clears a
  stranded reverse key on its own authority, so this module never touches
  ``_data``, and the write is ``save()``'s atomic tempfile + ``os.replace``.
* **Four guards, all pre-write.** Every check runs before ``save()``, so a
  refusal leaves the store byte-identical.

Exit codes: ``0`` healed or nothing to do, ``1`` a guard refused, ``2``
operational failure.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_AUDIT_BASENAME = "rebar-bridge-repair-audit.jsonl"
_ORPHAN_KIND = "reverse_missing_forward"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _audit_path(tracker: Path) -> str | None:
    from rebar._store.gitutil import _resolve_tracker_git_dir

    git_dir = _resolve_tracker_git_dir(str(tracker))
    return os.path.join(git_dir, _AUDIT_BASENAME) if git_dir else None


def _record_audit(tracker: Path, record: dict) -> str | None:
    """Append one JSON line to the tracker's durable repair log.

    Lives in the GIT DIR, not the worktree: the log must survive the operation
    without becoming store content a later merge could conflict on.
    """
    path = _audit_path(tracker)
    if path is None:
        return None
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError:
        return None
    return path


def _actor(tracker: Path) -> str:
    """Who ran this, for the audit line — the git identity over ``$USER``."""
    from rebar import config

    return config.resolve_os_actor(tracker)


def _retired_keys(tracker: Path) -> set[str]:
    """Jira keys tombstoned in ``bindings-retired.json``.

    Read straight off disk rather than through ``BindingStore``'s private
    ``_retired`` set. Both the dict form (key -> record) and the legacy list
    form are accepted; an unreadable file yields an empty set, which is safe
    because guard 2 only ever *blocks* a deletion.
    """
    path = tracker / ".bridge_state" / "bindings-retired.json"
    if not path.exists():
        return set()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return set()
    if isinstance(data, dict):
        return {str(key) for key in data}
    if isinstance(data, list):
        return {str(key) for key in data}
    return set()


def _refuse(message: str) -> int:
    sys.stderr.write(f"Refusing to repair: {message}\n")
    return 1


def prune_orphan_reverse_bindings(tracker_dir: Path, *, argv: list[str] | None = None) -> int:
    """Delete ``reverse`` keys with no forward entry. Returns a process exit code.

    Applies the four guards the 874a data repair established, all before any
    write, so a refusal leaves ``bindings.json`` untouched.
    """
    from rebar._engine_support import bridge_fsck

    tracker = Path(tracker_dir)
    try:
        findings = bridge_fsck.audit_store_integrity(tracker)
    except Exception as exc:  # noqa: BLE001 — an unreadable store is operational, not a refusal
        sys.stderr.write(f"Error: {exc}\n")
        return 2

    if not findings:
        print("Store integrity is clean; nothing to repair.")
        return 0

    # --- Guard 1: the audited set must be EXCLUSIVELY orphaned reverse keys. ---
    # Repairing a store that also carries a fault this verb does not understand
    # risks deleting evidence of the real problem.
    foreign = sorted({str(f.get("kind")) for f in findings if f.get("kind") != _ORPHAN_KIND})
    if foreign:
        return _refuse(
            f"store_integrity carries {', '.join(foreign)} alongside {_ORPHAN_KIND}. "
            "This verb prunes orphaned reverse keys only — resolve the other findings first."
        )

    orphans: dict[str, str] = {}
    for finding in findings:
        jira_key = finding.get("jira_key")
        local_id = finding.get("local_id")
        if not isinstance(jira_key, str) or not isinstance(local_id, str):
            return _refuse(
                f"a {_ORPHAN_KIND} finding has a non-string key/id "
                f"({jira_key!r} -> {local_id!r}); repair by hand."
            )
        orphans[jira_key] = local_id

    # --- Guard 2: a retired key means tombstone was the right call, not delete. ---
    retired = _retired_keys(tracker) & set(orphans)
    if retired:
        return _refuse(
            f"{', '.join(sorted(retired))} appear in bindings-retired.json — "
            "a retired binding is a tombstone, not an orphan."
        )

    # The reconciler ships as an embedded top-level ``rebar_reconciler`` package,
    # not as ``rebar._engine.*`` (there is no ``_engine/__init__.py``), so it is
    # reached through the supported loader rather than a plain import.
    from rebar._lib_ops import _engine_module

    binding_store = _engine_module("rebar_reconciler.binding_store")

    try:
        store = binding_store.BindingStore(tracker)
    except Exception as exc:  # noqa: BLE001 — _load fails CLOSED on a corrupt store, by design
        sys.stderr.write(f"Error: {exc}\n")
        return 2

    before = store.all_bindings()

    # --- Guard 3: no orphan may actually have a forward binding. ---
    # The audit and the store are read separately, so a store that changed
    # between the two reads must not be repaired against a stale finding set.
    bound = sorted(key for key, local_id in orphans.items() if store.is_bound(local_id))
    if bound:
        return _refuse(
            f"{', '.join(bound)} now have forward bindings — the store changed since "
            "the audit. Re-run `rebar bridge fsck` and try again."
        )

    for local_id in dict.fromkeys(orphans.values()):
        store.unbind(local_id)

    # --- Guard 4: verify IN MEMORY, before the write. ---
    after = store.all_bindings()
    if after != before:
        return _refuse(
            "the prune would have changed the forward map; refusing to write. "
            "This is a bug — please report it with the store contents."
        )
    residual = sorted(key for key in orphans if store.get_local_id(key) is not None)
    if residual:
        return _refuse(
            f"{', '.join(residual)} survived the prune; refusing to write a partial repair."
        )

    try:
        store.save()
    except OSError as exc:
        sys.stderr.write(f"Error: could not write bindings.json: {exc}\n")
        return 2

    record: dict[str, Any] = {
        "timestamp": _now_iso(),
        "actor": _actor(tracker),
        "argv": argv if argv is not None else sys.argv[1:],
        "tracker": str(tracker),
        "operation": "prune_orphan_reverse_bindings",
        "deleted_reverse_keys": sorted(orphans),
        "deleted_count": len(orphans),
        "forward_count": len(after),
    }
    audit_file = _record_audit(tracker, record)

    print(f"Pruned {len(orphans)} orphaned reverse binding(s): {', '.join(sorted(orphans))}")
    print(f"Forward bindings unchanged: {len(after)}")
    if audit_file:
        print(f"Audit: {audit_file}")
    return 0
