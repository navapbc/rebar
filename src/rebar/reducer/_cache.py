"""Ticket reducer file-level cache: read and write .cache.json."""

from __future__ import annotations

import hashlib
import json
import logging
import os

from rebar._store.fsutil import atomic_write

logger = logging.getLogger(__name__)

# Compaction preserves folded events as ``<name>.retired`` under invariant I1 in
# ``docs/concurrency.md``. The append-only source remains available without entering replay
# or fsck. This shared suffix keeps compaction and reducer scans aligned.
RETIRED_SUFFIX = ".retired"


def is_active_event(name: str) -> bool:
    """Return whether ``name`` is an active event rather than a retired source.

    Retired events have already entered a SNAPSHOT. Ordinary replay, directory hashes, and
    fsck omit them. Rebuild mode restores them explicitly.
    """
    return not name.endswith(RETIRED_SUFFIX)


# Event metadata does not reflect changed projections. Including this manual version in the
# directory hash invalidates older caches. Increment it whenever projection semantics change.
_REDUCER_CACHE_VERSION = 7


def _load_json(path: str) -> dict | None:
    """Load a single JSON object from ``path``; None on any read/parse error or non-dict."""
    try:
        with open(path, encoding="utf-8") as fh:
            obj = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    return obj if isinstance(obj, dict) else None


def _ondisk_attestation_kinds(ticket_dir: str, event_filenames: list[str]) -> set[str]:
    """Derive additive attestation kinds from active SIGNATURE and SNAPSHOT events.

    The result lets :func:`read_cache` reject cached attestations that conflict with the log
    under the validity-on-read policy. Other event names require no file reads.
    """
    from ._processors_identity import attestation_kind

    kinds: set[str] = set()
    for name in event_filenames:
        if name.endswith("-SIGNATURE.json"):
            ev = _load_json(os.path.join(ticket_dir, name))
            data = ev.get("data") if ev else None
            if not isinstance(data, dict):
                continue
            kind = attestation_kind(data.get("manifest"), data)
            if kind is not None:
                kinds.add(kind)
        elif name.endswith("-SNAPSHOT.json") and not name.endswith("-PRECONDITIONS-SNAPSHOT.json"):
            snap = _load_json(os.path.join(ticket_dir, name))
            data = snap.get("data") if snap else None
            compiled = data.get("compiled_state") if isinstance(data, dict) else None
            if not isinstance(compiled, dict):
                continue
            atts = compiled.get("attestations")
            if isinstance(atts, dict):
                kinds.update(atts.keys())
            else:
                # Legacy snapshot: a single kind-keyable ``signature`` folds into the map.
                sig = compiled.get("signature")
                if isinstance(sig, dict):
                    k = attestation_kind(sig.get("manifest"), {})
                    if k is not None:
                        kinds.add(k)
    return kinds


def read_cache(
    cache_path: str, dir_hash: str, ticket_dir: str, event_filenames: list[str]
) -> dict | None:
    """Return state when its hash and logged attestation kinds match.

    A missed reducer-version increment or cache written by another projection can retain a
    matching event hash with stale attestations. Comparing its keys with SIGNATURE and
    SNAPSHOT evidence forces recomputation.
    """
    cached = _load_json(cache_path)
    if not (cached and cached.get("dir_hash") == dir_hash):
        return None
    state = cached.get("state")
    if not isinstance(state, dict):
        return None
    cached_kinds = set((state.get("attestations") or {}).keys())
    if cached_kinds != _ondisk_attestation_kinds(ticket_dir, event_filenames):
        # Stale / old-projection attestation map: force a re-derive from the log.
        return None
    return state


def write_cache(cache_path: str, dir_hash: str, state: dict, ticket_dir: str) -> None:
    """Cache state atomically unless ``ticket_dir`` belongs to an immutable snapshot.

    A derived cache file would change janitor digests and can corrupt shared hardlinks. Snapshot
    reads therefore remain uncached under ADR 0005 D2 and ticket 5c27-7926.
    """
    # Deferred import: keep the reducer core decoupled from the snapshot subsystem
    # except at this one write seam.
    from rebar._snapshot.repo_snapshot import in_snapshot_entry

    if in_snapshot_entry(ticket_dir):
        return
    try:
        envelope = json.dumps({"dir_hash": dir_hash, "state": state}, ensure_ascii=False)
        atomic_write(cache_path, envelope)
    except OSError:
        logger.warning("failed to write cache for %s", ticket_dir, exc_info=True)


def compute_dir_hash(ticket_dir: str, event_filenames: list[str]) -> str:
    """Hash the reducer version and event names, sizes, and nanosecond mtimes.

    One stat per file detects additions, deletions, and same-size rewrites that names and sizes
    alone miss.
    """
    hash_parts: list[str] = [f"rv:{_REDUCER_CACHE_VERSION}"]
    for name in event_filenames:
        path = os.path.join(ticket_dir, name)
        try:
            st = os.stat(path)
            size, mtime_ns = st.st_size, st.st_mtime_ns
        except OSError:
            size, mtime_ns = -1, -1
        hash_parts.append(f"{name}:{size}:{mtime_ns}")
    hash_parts.append(
        "marker:present"
        if os.path.exists(os.path.join(ticket_dir, ".archived"))
        else "marker:absent"
    )
    return hashlib.sha256("|".join(hash_parts).encode()).hexdigest()


def prepare_event_files(
    ticket_dir: str,
    *,
    include_retired: bool = False,
) -> tuple[str, str, list[str], dict | None]:
    """Return cache metadata, sorted event paths, and cached state when present.

    Normal mode omits dotfiles and retired sources before reading the cache. Rebuild mode
    includes retired raw events, omits SNAPSHOT events, and bypasses the active-event cache to
    replay the entire event log.
    """
    from ._sort import event_sort_key

    cache_path = os.path.join(ticket_dir, ".cache.json")

    try:
        all_files = os.listdir(ticket_dir)
    except OSError:
        all_files = []

    def _is_event(name: str) -> bool:
        if name.startswith("."):  # .cache.json and any other dotfile
            return False
        if name.endswith(".json") and is_active_event(name):
            # Rebuild replays the raw log directly, so a SNAPSHOT (which would
            # short-circuit replay) is excluded from the set it rebuilds over.
            return not (include_retired and name.endswith("-SNAPSHOT.json"))
        # Rebuild also folds the append-only ``*.retired`` sources back in — except a
        # retired SNAPSHOT, which is likewise not a raw event to replay.
        return (
            include_retired
            and name.endswith(RETIRED_SUFFIX)
            and not name.endswith("-SNAPSHOT.json" + RETIRED_SUFFIX)
        )

    event_filenames = sorted(f for f in all_files if _is_event(f))
    dir_hash = compute_dir_hash(ticket_dir, event_filenames)

    # The rebuild path reads the full file set directly; never key it to (or serve it
    # from) the active-only reducer cache.
    cached = (
        None if include_retired else read_cache(cache_path, dir_hash, ticket_dir, event_filenames)
    )

    event_files = sorted(
        (os.path.join(ticket_dir, f) for f in event_filenames),
        key=event_sort_key,
    )

    return cache_path, dir_hash, event_files, cached
