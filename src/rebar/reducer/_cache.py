"""Ticket reducer file-level cache: read and write .cache.json."""

from __future__ import annotations

import hashlib
import json
import logging
import os

logger = logging.getLogger(__name__)

# Invariant I1 (docs/concurrency.md): compaction RENAMES the event files it folds
# to ``<name>.retired`` instead of hard-deleting them. A hard delete can be
# resurrected by a delete/add reconciliation (the RC1 rebase class) and then trips
# SNAPSHOT_INCONSISTENT; an append-only rename never loses the source bytes and is
# invisible to replay/fsck. This is the SINGLE source of truth for that suffix —
# compaction (the producer), the reducer (both listing paths), and fsck all import
# it so there is exactly one string literal to reason about.
RETIRED_SUFFIX = ".retired"


def is_active_event(name: str) -> bool:
    """True for a live event file, False for a folded ``*.retired`` source.

    A retired file is an append-only tombstone of an event that compaction has
    already folded into a SNAPSHOT; it must be excluded from every replay, dir
    hash, and fsck scan so it neither re-enters state nor reads as an
    inconsistency."""
    return not name.endswith(RETIRED_SUFFIX)


# Reducer-logic cache version. The dir-hash captures EVENT-FILE changes, but not
# changes to how the reducer PROJECTS those events into state. When the reducer's
# projection semantics change, every previously-cached .cache.json would
# otherwise serve a state compiled by the old logic. Folding this version into
# the dir hash invalidates all caches on a bump. BUMP THIS whenever a processor's
# projection changes.
#   v2: process_revert now un-archives on REVERT-of-ARCHIVED (bug vocal-jig-apron)
#   v3: replay now projects a derived `updated_at` (P1.1); pre-v3 caches lack it
#   v4: replay now projects the kind-keyed `attestations` map (epic
#       dark-acme-lumen); pre-v4 caches lack it, hiding a signed plan-review
#       attestation and wrongly blocking `claim` (bug wait-warp-inlay)
#   v5: replay now projects an identity's epoch-scoped `keyring` + `keyring_epoch`
#       (epic gnu-whale-ichor / e165); pre-v5 caches lack them
#   v6: keyring is now POSITION-based — records are {public_key, added_at, revoked_at}
#       and the `keyring_epoch` cursor is gone (epic gnu-whale-ichor, git-commit-ancestry
#       validity); pre-v6 caches hold stale epoch-era records
_REDUCER_CACHE_VERSION = 6


def read_cache(cache_path: str, dir_hash: str) -> dict | None:
    """Return the cached state if dir_hash matches, else None (cache miss)."""
    try:
        with open(cache_path, encoding="utf-8") as cf:
            cached = json.load(cf)
        if isinstance(cached, dict) and cached.get("dir_hash") == dir_hash:
            return cached["state"]
    except (OSError, json.JSONDecodeError, KeyError):
        pass
    return None


def write_cache(cache_path: str, dir_hash: str, state: dict, ticket_dir: str) -> None:
    """Atomically write the state to .cache.json using a tmp-then-rename pattern."""
    try:
        cache_tmp = cache_path + ".tmp"
        with open(cache_tmp, "w", encoding="utf-8") as tf:
            json.dump({"dir_hash": dir_hash, "state": state}, tf, ensure_ascii=False)
        os.rename(cache_tmp, cache_path)
    except OSError:
        logger.warning("failed to write cache for %s", ticket_dir, exc_info=True)


def compute_dir_hash(ticket_dir: str, event_filenames: list[str]) -> str:
    """Compute a content hash based on event filenames, sizes, and mtimes.

    Uses filename + file size + modification time (a single fast stat per file)
    to detect additions/deletions and in-place content overwrites — including a
    same-byte-length rewrite (e.g. from a git checkout/rebase of the tickets
    branch or an fsck-recover cherry-pick), which a filename+size key alone
    cannot see. Folding in st_mtime_ns closes that stale-read gap.
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
    """Build sorted event file list and compute dir_hash; check cache.

    Returns (cache_path, dir_hash, event_files, cached_state_json_or_none).
    cached_state_json_or_none is the raw cached state dict if a cache hit
    occurred, or None if a cache miss (caller must recompute).

    Normal mode (``include_retired=False``): ``event_files`` is the sorted list of
    active ``*.json`` event file paths (dotfiles and folded ``*.retired`` sources
    excluded), and the reducer cache is consulted.

    Rebuild mode (``include_retired=True``, RC2b Option 1): the folded ``*.retired``
    tombstones are folded back into the set and every ``SNAPSHOT`` file is *excluded*,
    so the full ordered history replays from scratch (no snapshot short-circuit) —
    this reconstructs state that a stale snapshot's positional skip had silently
    dropped. This path **bypasses the cache entirely** (it reads the full file set
    directly and must never return a stale ``*.json``-only cache entry).
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
    cached = None if include_retired else read_cache(cache_path, dir_hash)

    event_files = sorted(
        (os.path.join(ticket_dir, f) for f in event_filenames),
        key=event_sort_key,
    )

    return cache_path, dir_hash, event_files, cached
