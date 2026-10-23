#!/usr/bin/env python3
"""Jira→local field/event translation for the inbound applier path.
This Jira-sync layer decodes rich text through the Backend port and writes
one uncommitted event file under the store lock per call. Reconciler
orchestration commits the batch. General local bulk writes use rebar._store."""

from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path
from typing import Any

from rebar.reconciler.inbound_fields import _LOCAL_STATUS_VOCAB, recover_status_label  # noqa: F401

# Map Jira issuetype -> local ticket_type. Anything else falls through to 'task'.
_JIRA_TYPE_MAP: dict[str, str] = {
    "Bug": "bug",
    "Story": "story",
    "Task": "task",
    "Epic": "epic",
    "Sub-task": "task",
}

# Status labels restore local status before raw Jira workflow mapping.
# Bridge labels stay out of imported local tags. The imported tag is local only.
_BRIDGE_INTERNAL_TAG_PREFIXES: tuple[str, ...] = (
    "rebar-id:",
    "rebar-id-",
    "rebar-status:",
    "rebar-type:",
)

_JIRA_PRIORITY_MAP: dict[str, int] = {
    "Highest": 0,
    "High": 1,
    "Medium": 2,
    "Low": 3,
    "Lowest": 4,
}

_VALID_PRIORITY_RANGE = range(0, 5)  # 0-4 inclusive

# Local status vocabulary. Single-sourced from ``inbound_fields._LOCAL_STATUS_VOCAB``
# (the inbound recovery domain) so this documentation constant cannot drift from the
# set the recovery helper actually honours — the prior hand-maintained literal listed a
# stale ``done`` and omitted ``deleted``. Listed here for documentation / debug purposes
# only — the typed-mutation inbound path no longer uses value-membership to decide
# whether to invoke the Jira->local mapper (see ``_apply_inbound_update``).
_LOCAL_STATUS_VALUES: tuple[str, ...] = tuple(sorted(_LOCAL_STATUS_VOCAB))


def _resolve_priority(raw_pri: Any) -> int:
    """Convert a Jira priority (name-string or int) to a local 0-4 integer.

    Integers outside 0-4 are clamped to the default (2 / Medium).
    Unrecognised name strings also fall back to 2.
    """
    if isinstance(raw_pri, int):
        return raw_pri if raw_pri in _VALID_PRIORITY_RANGE else 2
    pri_name = _extract_name(raw_pri)
    return _JIRA_PRIORITY_MAP.get(pri_name, 2)


def _jira_key_to_local_id(jira_key: str) -> str:
    """DIG-123 -> jira-dig-123. Idempotent for already-prefixed local ids."""
    if jira_key.startswith("jira-"):
        return jira_key
    return "jira-" + jira_key.lower()


def _jira_status_to_local(jira_status: str) -> str:
    """Map Jira workflow status through the canonical reverse map.
    Annotation labels take precedence at the caller for lossy mappings such as
    blocked and cancelled. Unknown statuses become open."""
    if not jira_status:
        return "open"
    try:
        # Late-load config without polluting module namespace.
        cfg_mod = importlib.import_module("rebar.reconciler.config")
        mapping = getattr(cfg_mod, "jira_to_local_status", {}) or {}
    except Exception:  # noqa: BLE001 — fail-open: a missing/broken status-mapping config defaults the inbound status to open
        return "open"
    return mapping.get(jira_status, "open")


def _event_meta() -> tuple[int, str, str, str]:
    """Return (timestamp_ns, uuid4_str, env_id, author) for a new event."""
    import time as _time
    import uuid as _uuid

    # Resolve event identity through the composition root. This absolute import
    # also works when tests load this module by path.
    from rebar.config import reconciler_event_identity

    env_id, author = reconciler_event_identity()
    return (
        _time.time_ns(),
        str(_uuid.uuid4()),
        env_id,
        author,
    )


def _resolve_tracker_dir(repo_root: Path | None) -> Path:
    """Return the configured ticket-store path through tracker_dir.
    The environment override wins over an absolute or root-relative tracker.dir."""
    from rebar.config import tracker_dir

    if repo_root is None:
        from rebar.config import reconciler_repo_root as _owned_repo_root

        repo_root = _owned_repo_root()
    return tracker_dir(repo_root)


def _read_latest_status(tracker_dir: Path, ticket_id: str) -> str:
    """Read ticket status through the canonical reducer for optimistic concurrency.
    It handles compacted snapshots and status forks. Missing or unreadable
    tickets fall back to open."""
    ticket_dir = tracker_dir / ticket_id
    if not ticket_dir.is_dir():
        return "open"
    try:
        state = _load_ticket_reducer().reduce_ticket(str(ticket_dir))
    except Exception:  # noqa: BLE001 — stay tolerant, mirror the reducer's default
        return "open"
    status = state.get("status") if isinstance(state, dict) else None
    # An error/fsck_needed projection has no real status; fall back to the
    # reducer's initial state rather than pushing a sentinel to Jira.
    if not isinstance(status, str) or status in ("", "error", "fsck_needed"):
        return "open"
    return status


def _write_event_file(
    tracker_dir: Path, ticket_id: str, event_type: str, data: dict[str, Any]
) -> Path:
    """Atomically write one canonical event file under the shared store lock.
    The reconciler later commits it with its batch. Return the event path."""
    from rebar._store import event_append as _store_event_append
    from rebar._store import hlc as _hlc
    from rebar._store import lock as _store_lock
    from rebar._store import staging as _staging
    from rebar._store.canonical import canonical_str

    _, uuid_str, env_id, author = _event_meta()
    ts = _hlc.next_tick(str(tracker_dir), ticket_id)
    event = {
        "timestamp": ts,
        "uuid": uuid_str,
        "event_type": event_type,
        "env_id": env_id,
        "author": author,
        "data": data,
    }
    # Use shared attribution and signing helpers while leaving the event
    # uncommitted for reconciler orchestration. Missing identity or signing
    # material permits an unsigned event.
    from rebar._commands import _seam

    # Finalize through the shared seam for attribution, signing, and secret
    # screening. A refused Jira-sourced event fails before publication.
    _repo_root = tracker_dir.parent
    _seam.finalize_event(event, ticket_id, event_type, data, str(tracker_dir), _repo_root)
    # Stage the directory and event after screening, then publish in one
    # rename. A refusal or lock timeout leaves no empty ticket directory.
    filename = _store_event_append.event_filename(ts, uuid_str, event_type)
    staged = _staging.stage_event(
        str(tracker_dir), ticket_id, filename, canonical_str(event).encode("utf-8")
    )
    # attempts=1 preserves the bare module's historical single-shot acquire. The
    # bare ``event_append.write_lock`` re-raised a lock timeout as the builtin
    # ``TimeoutError``; preserve that contract here so callers see the same type.
    try:
        with _store_lock.write_lock(str(tracker_dir), attempts=1, dual_window=True):
            staged.promote()
    except _store_lock.LockTimeout as exc:
        raise TimeoutError(str(exc)) from None
    finally:
        staged.discard()  # no-op once published
    return Path(staged.final_path)


def _extract_name(val, default=""):
    """Extract name or displayName from a Jira field object. Return a raw
    string unchanged."""
    if isinstance(val, dict):
        return val.get("name") or val.get("displayName") or default
    return val or default


_TICKET_REDUCER_MODULE = None


def _load_ticket_reducer():
    """Load the canonical reducer only when a branch needs local status or tags."""
    global _TICKET_REDUCER_MODULE
    if _TICKET_REDUCER_MODULE is not None:
        return _TICKET_REDUCER_MODULE
    import rebar.reducer as _tr

    _TICKET_REDUCER_MODULE = _tr
    return _tr


def _normalize_adf_body(body: Any, inbound_mapper: Any | None = None) -> str:
    """Normalize a Jira description to text through the injected inbound mapper.
    A missing mapper resolves from the configured backend when called. This
    prevents ADF dictionaries from entering local EDIT events."""
    if inbound_mapper is None:
        from rebar.config import compose_config
        from rebar.reconciler._backend_registry import select_backend

        inbound_mapper = select_backend(compose_config()).inbound
    return inbound_mapper.normalize_rich_text(body)
