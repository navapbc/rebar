"""Provide the create-time advisory for recent duplicate titles.

A store-side journal records normalized titles within a fixed window. Each create prunes and
probes the journal before appending its row, avoiding a store scan. One exclusive ``fcntl.flock``
protects the read, prune, probe, append, and rewrite sequence without nesting under the store
write lock. Journal order therefore makes a concurrent second create observe the first.

The feature runs only when ``verify.suggest_duplicate_tickets`` is enabled. Configuration and
journal failures suppress the advisory without affecting the completed create. Candidate
reduction failures keep the warning but report its status as ``unknown``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

# Imported at module scope so tests substitute the probe by patching this name on this
# module -- the same seam the normalized-title probe's own tests already use.
from rebar.reducer._api import _NON_GRAPH_ARTIFACT_TYPES
from rebar.similar import find_similar

logger = logging.getLogger("rebar")

_JOURNAL_NAME = "recent-creates.json"
_LOCK_NAME = "recent-creates.lock"

#: How long a create stays probe-visible. 600 s is ~3.5x the largest observed true-duplicate
#: delta (171 s) from the eac3 store measurement; the same measurement found same-title pairs
#: outside a short window are legitimate (recurring filings, fixtures), so a wider window
#: would only buy false positives.
RECENT_WINDOW_NS = 600 * 1_000_000_000

_NON_ALNUM = re.compile(r"[^0-9a-z]+")


def normalize_title(title: str) -> str:
    """Fold case, punctuation, and whitespace so cosmetic variants compare equal."""
    return " ".join(_NON_ALNUM.sub(" ", title.lower()).split())


@contextmanager
def _journal_lock(rebar_dir: str) -> Iterator[None]:
    """A dedicated, local exclusive lock for the journal RMW — held only for the one
    small-file read-modify-write, never across the store write lock (no ordering hazard)."""
    from rebar._store.fsutil import sibling_exclusive_lock

    with sibling_exclusive_lock(os.path.join(rebar_dir, _JOURNAL_NAME), lock_name=_LOCK_NAME):
        yield


def _read_entries(journal_path: str) -> list[dict[str, Any]]:
    """The journal's well-formed entries, or ``[]`` when anything about it cannot be
    trusted — absent, unreadable, not JSON, wrong shape. The caller's fallback (treat the
    window as empty) is always correct, so nothing here raises."""
    try:
        with open(journal_path, encoding="utf-8") as fh:
            payload = json.load(fh)
    except Exception:  # noqa: BLE001 — an untrusted journal degrades to an empty window
        return []
    entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        return []
    kept: list[dict[str, Any]] = []
    for entry in entries:
        if (
            isinstance(entry, dict)
            and isinstance(entry.get("ts_ns"), int)
            and not isinstance(entry.get("ts_ns"), bool)
            and isinstance(entry.get("id"), str)
            and isinstance(entry.get("title_norm"), str)
        ):
            kept.append(entry)
    return kept


def _candidate_status(tracker: Any, ticket_id: str) -> str:
    """The candidate's LIVE status via a single-ticket reduce (O(1), one dir read);
    ``unknown`` on any failure — the advisory is still worth emitting without it."""
    try:
        from rebar.reducer import reduce_ticket

        status = (reduce_ticket(os.path.join(str(tracker), ticket_id)) or {}).get("status")
        return status if isinstance(status, str) and status else "unknown"
    except Exception:  # noqa: BLE001 — best-effort: the advisory is still worth emitting
        return "unknown"


def _probe_and_record(
    journal_path: str, *, ticket_id: str, alias: str, title: str, now_ns: int
) -> list[dict[str, Any]]:
    """One locked RMW body: prune to the window, collect same-title matches, append this
    create's own entry, rewrite atomically. Returns the matches (possibly empty)."""
    from rebar._store.fsutil import atomic_write

    title_norm = normalize_title(title)
    entries = [
        e
        for e in _read_entries(journal_path)
        if 0 <= now_ns - e["ts_ns"] < RECENT_WINDOW_NS and e["id"] != ticket_id
    ]
    matches = [e for e in entries if title_norm and e["title_norm"] == title_norm]
    entries.append(
        {
            "ts_ns": now_ns,
            "id": ticket_id,
            "alias": alias,
            "title": title,
            "title_norm": title_norm,
        }
    )
    atomic_write(journal_path, json.dumps({"entries": entries}), encoding="utf-8")
    return matches


def duplicate_create_warning(
    tracker: Any, *, ticket_id: str, alias: str | None, title: str, cfg_root: str
) -> str | None:
    """Return warning text for a recent normalized-title match, or ``None``.

    The probe runs after CREATE has landed. The duplicate-suggestion setting governs this
    probe. CLI, library, and MCP callers emit the advisory
    through their own channels.
    """
    try:
        from rebar.config import compose_config

        if not compose_config(cfg_root).verify.suggest_duplicate_tickets:
            return None

        from rebar._store.paths import StorePaths

        paths = StorePaths(str(tracker))
        now_ns = time.time_ns()
        with _journal_lock(paths.rebar_dir):
            matches = _probe_and_record(
                paths.sidecar(_JOURNAL_NAME),
                ticket_id=ticket_id,
                alias=alias or "",
                title=title,
                now_ns=now_ns,
            )
        if not matches:
            return None
        candidate = max(matches, key=lambda e: e["ts_ns"])
        cand_ref = candidate.get("alias") or candidate["id"]
        status = _candidate_status(tracker, candidate["id"])
        age_s = max(0, (now_ns - candidate["ts_ns"]) // 1_000_000_000)
        new_ref = alias or ticket_id
    except Exception:  # an advisory notice must never fail a completed write
        logger.debug("duplicate-create probe failed for %s", ticket_id, exc_info=True)
        return None
    return (
        f"possible duplicate: '{title}' matches the title of {cand_ref} "
        f"(status {status}, created {age_s}s ago). Advisory only — {new_ref} was still "
        f"created; if it duplicates {cand_ref}, record it with: "
        f"rebar link {new_ref} {cand_ref} duplicates"
    )


def similar_tickets_advisory(
    tracker: Any,
    *,
    ticket_id: str,
    ticket_type: str,
    title: str,
    description: str | None,
    cfg_root: str,
) -> list[dict[str, Any]]:
    """Ranked tickets resembling a create that has already landed, or ``[]``.

    The normalized-title probe above catches only byte-identical titles. This one ranks
    by text, so a duplicate filed in different words is visible too.

    Non-graph artifact types are skipped: a ranked list of similar *tickets* means
    nothing for a session log, a code-review artifact or an identity, and probing would
    force an index catch-up on a write that never touches the ticket graph. The
    classification is the one those types already carry elsewhere, not a new one.

    The new ticket's own id is excluded, because catch-up has indexed it by now and it
    would otherwise rank as its own top match.
    """
    try:
        if ticket_type in _NON_GRAPH_ARTIFACT_TYPES:
            return []
        from rebar.config import compose_config

        if not compose_config(cfg_root).verify.suggest_duplicate_tickets:
            return []
        return find_similar(
            str(tracker),
            title=title,
            description=description or "",
            exclude_id=ticket_id,
        )
    except Exception:  # an advisory notice must never fail a completed write
        logger.debug("similar-ticket probe failed for %s", ticket_id, exc_info=True)
        return []


def format_similar_tickets(results: object, *, new_ref: str) -> str | None:
    """Render :func:`similar_tickets_advisory` output for a stderr channel, or ``None``.

    ``None`` for anything empty or unrecognized, so a caller emits nothing rather than an
    empty heading. ``score`` runs 0 to 1 with 1 most relevant.
    """
    if not isinstance(results, list) or not results:
        return None
    lines = [
        f"  {r.get('alias') or r.get('id')}  (score {float(r.get('score', 0.0)):.2f})  "
        f"{r.get('title', '')}"
        for r in results
        if isinstance(r, dict)
    ]
    if not lines:
        return None
    return (
        "possibly similar tickets (score 0-1, 1 most relevant):\n"
        + "\n".join(lines)
        + f"\nAdvisory only — {new_ref} was still created; if it duplicates one of these, "
        f"record it with: rebar link {new_ref} <id> duplicates"
    )
