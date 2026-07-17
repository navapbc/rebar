#!/usr/bin/env python3
"""Jira→local field/event translation for the inbound applier path.

Pure translation + local-event-store IO helpers used by the inbound leaf
appliers (``apply_inbound``): map Jira issuetype/priority/status to their local
forms, normalize ADF bodies, and append local ticket events. No Jira writes
happen here — this is the parse/serialize layer beneath the inbound appliers.

NOTE: ``_write_event_file`` here writes ONE inbound event file per call under the
store lock (``os.replace``; no ``git add``/``commit`` of its own — it does NOT use
``stage_and_commit``); the reconciler pass commits via its own orchestration. This
is a **Jira-sync internal** — NOT the general local-store batch-write mechanism (and
``applier._apply_batch`` is an OUTBOUND Jira REST sequencer, not a local
commit-batcher). Local bulk writes (import/export/migration) must not route through
here; the local batch-write primitive lives in ``rebar._store``. See
``docs/architecture.md`` "Two writers, one store".

Event writes and reducer reads now go through the in-package store primitives
(``rebar._store`` / ``rebar.reducer``) directly — Tier E E5b dropped the bare
``event_append`` / ``ticket_reducer`` compat shims and their ``sys.path`` dances.
The remaining lazy loader (``_load_adf_module``) preserves the engine's by-path
loading for the sibling ``adf`` module.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from typing import Any, overload


@overload
def _rebar_env(name: str, default: str) -> str: ...


@overload
def _rebar_env(name: str, default: None = None) -> str | None: ...


def _rebar_env(name: str, default: str | None = None) -> str | None:
    """Read ``REBAR_<name>`` from the environment.

    Local to this module (each reconciler module keeps its own copy): the
    reconciler modules are spec-loaded under test where a cross-module import of a
    shared shim would not resolve.
    """
    return os.environ.get(f"REBAR_{name}", default)


# Map Jira issuetype -> local ticket_type. Anything else falls through to 'task'.
_JIRA_TYPE_MAP: dict[str, str] = {
    "Bug": "bug",
    "Story": "story",
    "Task": "task",
    "Epic": "epic",
    "Sub-task": "task",
}

# rebar-status: annotation labels override the Jira workflow status on inbound
# (blocked/cancelled have no live DIG workflow equivalent; the label is the
# lossless encoding). Mirrors inbound_differ._REBAR_STATUS_LABEL_TO_LOCAL —
# _apply_inbound_create applies the same precedence so a freshly imported
# issue lands at the SAME local status the bound-ticket inbound differ would
# compute on the next pass (ticket robe-creek-zealot).
_REBAR_STATUS_LABEL_TO_LOCAL: dict[str, str] = {
    "rebar-status:blocked": "blocked",
    "rebar-status:cancelled": "cancelled",
}

# Bridge-internal label prefixes that must never leak into local ticket tags
# at import time. Matches inbound_differ._EXCLUDED_PREFIXES minus "imported:"
# (which is local-only and appended below, never present on Jira).
_BRIDGE_INTERNAL_TAG_PREFIXES: tuple[str, ...] = (
    "rebar-id:",
    "rebar-id-",
    "rebar-status:",
)

_JIRA_PRIORITY_MAP: dict[str, int] = {
    "Highest": 0,
    "High": 1,
    "Medium": 2,
    "Low": 3,
    "Lowest": 4,
}

_VALID_PRIORITY_RANGE = range(0, 5)  # 0-4 inclusive

# Local status vocabulary (source of truth: ticket_reducer/_processors.py:process_status).
# Listed here for documentation / debug purposes only — the typed-mutation
# inbound path no longer uses value-membership to decide whether to invoke
# the Jira→local mapper (see _apply_inbound_update). Kept as a module
# constant so any future check is consistent with the reducer.
_LOCAL_STATUS_VALUES: tuple[str, ...] = (
    "idea",
    "open",
    "in_progress",
    "blocked",
    "closed",
    "cancelled",
    "done",
)


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
    """Reverse-map a Jira status to a local status using config.jira_to_local_status.

    Uses the CANONICAL reverse map, not an inversion of local_to_jira_status.
    The forward map is non-injective (blocked/in_progress → "In Progress";
    closed/cancelled/deleted → "Done"), and the pre-fix lexicographic
    tie-break over the inverted map picked the WRONG preimage: imported
    "In Progress" issues materialised locally as blocked and "Done" issues
    as cancelled (ticket robe-creek-zealot). blocked/cancelled are encoded
    losslessly via rebar-status: annotation labels — callers that have the
    issue's labels in hand (e.g. _apply_inbound_create) apply that override
    BEFORE consulting this workflow-status map, mirroring
    inbound_differ._map_jira_to_local_fields.

    Unknown / unmapped statuses fall back to "open".
    """
    if not jira_status:
        return "open"
    try:
        # Late-load config without polluting module namespace.
        config_path = Path(__file__).parent / "config.py"
        spec = importlib.util.spec_from_file_location("rebar_reconciler_config", config_path)
        if spec is None or spec.loader is None:
            return "open"
        cfg_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cfg_mod)  # type: ignore[union-attr]
        mapping = getattr(cfg_mod, "jira_to_local_status", {}) or {}
    except Exception:  # noqa: BLE001 — fail-open: a missing/broken status-mapping config defaults the inbound status to open
        return "open"
    return mapping.get(jira_status, "open")


def _event_meta() -> tuple[int, str, str, str]:
    """Return (timestamp_ns, uuid4_str, env_id, author) for a new event."""
    import time as _time
    import uuid as _uuid

    # Function-body ABSOLUTE import (story e622): this module is spec-loaded by
    # path in tests, so a sibling `rebar_reconciler.*` import would not resolve —
    # but an absolute `rebar.*` import does. The "reconciler" defaults are the
    # legacy-Jira signature the reducer's inference keys on; sourcing them from
    # the single source of truth keeps the writer and the inference in lockstep.
    from rebar.reducer._version import LEGACY_JIRA_AUTHOR, LEGACY_JIRA_ENV_ID

    return (
        _time.time_ns(),
        str(_uuid.uuid4()),
        _rebar_env("ENV_ID", LEGACY_JIRA_ENV_ID),
        _rebar_env("AUTHOR", LEGACY_JIRA_AUTHOR),
    )


def _resolve_tracker_dir(repo_root: Path | None) -> Path:
    """Resolve the .tickets-tracker directory. Honours the REBAR_TRACKER_DIR override."""
    from rebar.config import tracker_dir_override

    override = tracker_dir_override()
    if override:
        return Path(override)
    if repo_root is None:
        repo_root = Path(os.environ.get("REBAR_ROOT") or Path(__file__).resolve().parents[4])
    return Path(repo_root) / ".tickets-tracker"  # tickets-boundary-ok


def _read_latest_status(tracker_dir: Path, ticket_id: str) -> str:
    """Return the current status of ``ticket_id`` per the CANONICAL reducer.

    Used as the optimistic-concurrency ``current_status`` of the STATUS event the
    reconciler pushes, so it must match what every clone's reducer computes. A
    previous raw "last STATUS file wins" scan diverged from the reducer on two
    real shapes (ticket vary-ion-fry):
      * a COMPACTED ticket — its STATUS events are folded into a SNAPSHOT and the
        standalone files are gone, so the scan returned "open";
      * a STATUS FORK — resolved by lexically-lower event UUID, not file order.
    Delegating to ``reduce_ticket`` removes the divergence (it handles SNAPSHOT
    folding and UUID fork resolution).

    Tolerant of missing / unreadable / error tickets — returns ``"open"`` (the
    reducer's initial state) in those cases.
    """
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
    """Write a single ticket event JSON file under the unified write lock.

    Acquires the canonical ``.ticket-write.lock`` (I5) via
    :func:`rebar._store.lock.write_lock` — both the ``fcntl`` and ``mkdir`` legs,
    so the reconciler mutually excludes local leaf-writes on every platform (the
    pokey-matte-flute / stiff-mop-lane fixes) — then atomically writes ONE event
    file under the shared I2 filename contract (temp file + ``os.replace``).

    Tier E E5b: this replaced the bare ``_engine/event_append`` import (resolved by
    a ``sys.path`` dance) with the in-package store primitives. The on-disk event
    bytes go through the single canonical serializer
    ``rebar._store.canonical.canonical_str`` (sorted keys, P1.0) — byte-identical to
    every other live writer; re-serialisation is replay-safe (the reducer reads
    parsed keys, not bytes). Returns the path.
    """
    from rebar._store import event_append as _store_event_append
    from rebar._store import hlc as _hlc
    from rebar._store import lock as _store_lock
    from rebar._store.canonical import canonical_str

    _, uuid_str, env_id, author = _event_meta()
    ts = _hlc.next_tick(str(tracker_dir), ticket_id)
    ticket_dir = tracker_dir / ticket_id
    ticket_dir.mkdir(parents=True, exist_ok=True)
    event = {
        "timestamp": ts,
        "uuid": uuid_str,
        "event_type": event_type,
        "env_id": env_id,
        "author": author,
        "data": data,
    }
    # Authenticated authorship (story 9e76): stamp attribution (author_email / author_id) and
    # sign (author_sig) INLINE, reusing the same seam helpers as _seam.append_event, so
    # Jira-sourced reconciler events are attributable + verifiable under `rebar verify-identity`
    # instead of classifying as unknown-author/unsigned. Done INLINE rather than by routing
    # through _seam.append_event because append_event commits+pushes eagerly (write_and_push),
    # which would break the reconciler's batch-commit orchestration — this function deliberately
    # writes an UNCOMMITTED file. Best-effort + additive: with no resolvable identity or no
    # signing key the event is written unsigned (older readers ignore the extra keys); a signing
    # failure is logged, never raised. repo_root is the tracker worktree's parent.
    from rebar._commands import _seam

    _repo_root = tracker_dir.parent
    event.update(_seam.attribution_fields(_repo_root))
    _seam._apply_authorship(event, ticket_id, event_type, data, str(tracker_dir), _repo_root)
    final = ticket_dir / _store_event_append.event_filename(ts, uuid_str, event_type)
    tmp = ticket_dir / f".tmp-{uuid_str}-{event_type}"
    # attempts=1 preserves the bare module's historical single-shot acquire. The
    # bare ``event_append.write_lock`` re-raised a lock timeout as the builtin
    # ``TimeoutError``; preserve that contract here so callers see the same type.
    try:
        with _store_lock.write_lock(str(tracker_dir), attempts=1, dual_window=True):
            tmp.write_text(canonical_str(event), encoding="utf-8")
            os.replace(tmp, final)
    except _store_lock.LockTimeout as exc:
        raise TimeoutError(str(exc)) from None
    return final


def _extract_name(val, default=""):
    """Extract .name or .displayName from a nested Jira field object.

    Jira REST API returns many fields as nested objects (e.g.
    ``{"name": "Bug", "id": "10002"}``). This helper extracts the human-readable
    name, falling back to the raw value when it is already a string.
    """
    if isinstance(val, dict):
        return val.get("name") or val.get("displayName") or default
    return val or default


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


_ADF_KEY_APPLIER = "rebar_reconciler.adf"
_AdfModule_Applier = None


def _load_adf_module():
    """Lazy-load the sibling adf module (mirrors inbound_differ._load_adf)."""
    global _AdfModule_Applier
    if _AdfModule_Applier is None:
        _AdfModule_Applier = lazy_load(_ADF_KEY_APPLIER, "adf.py")
    return _AdfModule_Applier


_TICKET_REDUCER_MODULE = None


def _load_ticket_reducer():
    """Lazy-load the canonical reducer (``rebar.reducer``).

    Used to read the current local state (status / tags) before composing an
    inbound event. Loaded lazily so test contexts that never hit those branches
    are not forced to import the reducer package.

    Tier E E5b: replaced the bare ``ticket_reducer`` compat shim (resolved by a
    ``sys.path`` dance to the engine dir) with the in-package ``rebar.reducer`` —
    the shim was already a thin re-export of it, so ``reduce_ticket`` is identical.
    """
    global _TICKET_REDUCER_MODULE
    if _TICKET_REDUCER_MODULE is not None:
        return _TICKET_REDUCER_MODULE
    import rebar.reducer as _tr  # noqa: PLC0415 — lazy import by design

    _TICKET_REDUCER_MODULE = _tr
    return _tr


def _normalize_adf_body(body: Any) -> str:
    """Coerce a Jira description (ADF dict or string) to plain text.

    Defense-in-depth: the inbound differ should normalize ADF before
    surfacing the field, but a raw ADF dict on the wire here would
    otherwise be written verbatim into an EDIT event's ``description``
    slot — corrupting the local ticket store (reducer would surface a
    dict where a string is expected). See bug 1bb2-5da5.
    """
    if isinstance(body, dict):
        return _load_adf_module().adf_to_text(body)
    return str(body) if body is not None else ""
