"""Override a clone left behind by a remote-anchored history reclaim (ADR 0106 §5).

A reclaim publish-swap replaces the shared ``tickets`` history with a rewritten baseline,
so every clone that has not converged since shares no commit with the remote. The store
epoch record proves the relationship: the rewritten baseline carries a new ``epoch`` whose
``epoch_lineage`` names the epoch it replaced (:func:`compat.remote_epoch_supersedes_local`).
Only then does :mod:`rebar._store.sync` call :func:`override_onto_reclaimed_remote`, under
the store write lock it already holds.

The override never merges the old history back (that would resurrect reclaimed events).
It backs up the local tip to a local-only ref, resets to the pinned remote commit, and then:

* a **fresh** clone — whose oldest unpushed event is inside the reclaim horizon — re-adds
  its unpushed event files byte-identically in one local commit, which the normal push path
  delivers;
* a **stale** clone — whose oldest unpushed event predates the horizon — replays nothing.
  ADR 0106 §5 accepts that loss; the backup ref keeps the discarded work recoverable locally.

An event is *unpushed* only when its uuid is unknown to the remote tip: absent as a file
under its ticket directory (any suffix) and absent from every remote SNAPSHOT's
``data.source_event_uuids`` for that ticket. Local SNAPSHOT events are derived state and are
never replayed.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

logger = logging.getLogger(__name__)

#: Any ``(path, *args) -> CompletedProcess`` git runner, supplied by the caller so the
#: caller's bounded, patchable seam runs every git command.
GitRunner = Callable[..., subprocess.CompletedProcess]

#: Local-only refs that keep an overridden clone's previous tip recoverable.
BACKUP_REF_PREFIX = "refs/rebar/stale-override/"

# I2 filename contract: ``{timestamp}-{uuid}-{TYPE}.json``; compaction appends ``.retired``.
_EVENT_RE = re.compile(
    r"^(?P<ts>\d+)-(?P<uuid>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{12})-(?P<type>[A-Z_]+)\.json(?P<retired>\.retired)?$"
)


class StaleOverrideError(RuntimeError):
    """A git step of the override failed; the caller keeps its refusal behaviour."""


@dataclass(frozen=True)
class _Event:
    path: str
    ticket_dir: str
    uuid: str
    ts: int
    event_type: str
    retired: bool

    @property
    def live_path(self) -> str:
        return self.path.removesuffix(".retired")


@dataclass(frozen=True)
class OverrideOutcome:
    """What one override did: the backup ref and the event paths replayed or discarded."""

    stale: bool
    backup_ref: str
    remote_commit: str
    replayed: tuple[str, ...]
    discarded: tuple[str, ...]


def override_onto_reclaimed_remote(
    git: GitRunner,
    tracker: str | os.PathLike[str],
    remote_commit: str,
    *,
    horizon_days: int,
    now: datetime | None = None,
) -> OverrideOutcome:
    """Back up HEAD, reset to ``remote_commit``, and replay unpushed events if fresh.

    The caller must hold the store write lock and must already have established that
    the remote epoch supersedes the local one.
    """
    base = os.fspath(tracker)
    head = _required(git, base, "rev-parse", "--verify", "HEAD").strip()
    unpushed = _unpushed_events(git, base, head, remote_commit)
    cutoff = _cutoff_ns(now, horizon_days)
    stale = bool(unpushed) and min(event.ts for event in unpushed) < cutoff

    blobs = {} if stale else _blob_ids(git, base, head, unpushed)

    backup_ref = BACKUP_REF_PREFIX + _stamp(now)
    _required(git, base, "update-ref", backup_ref, head)
    _required(git, base, "reset", "--hard", remote_commit, "--quiet")

    if stale:
        logger.warning(
            "tickets sync: stale clone overridden by reclaimed remote %s — discarded %d "
            "unpushed event(s) older than the %d-day reclaim horizon; previous tip kept at %s",
            remote_commit,
            len(unpushed),
            horizon_days,
            backup_ref,
        )
        return OverrideOutcome(True, backup_ref, remote_commit, (), _paths(unpushed))

    if blobs:
        _replay(git, base, blobs)
    logger.warning(
        "tickets sync: clone moved onto reclaimed remote %s; replayed %d unpushed event(s); "
        "previous tip kept at %s",
        remote_commit,
        len(blobs),
        backup_ref,
    )
    return OverrideOutcome(False, backup_ref, remote_commit, tuple(sorted(blobs)), ())


def _unpushed_events(git: GitRunner, base: str, head: str, remote: str) -> list[_Event]:
    local = [e for e in _events(_tree_paths(git, base, head)) if e.event_type != "SNAPSHOT"]
    if not local:
        return []
    remote_paths = _tree_paths(git, base, remote)
    known: dict[str, set[str]] = {}
    for event in _events(remote_paths):
        known.setdefault(event.ticket_dir, set()).add(event.uuid)
    candidate_dirs = {event.ticket_dir for event in local}
    for event in _events(remote_paths):
        if event.event_type == "SNAPSHOT" and event.ticket_dir in candidate_dirs:
            known[event.ticket_dir] |= _snapshot_sources(git, base, remote, event.path)
    return [e for e in local if e.uuid not in known.get(e.ticket_dir, set())]


def _snapshot_sources(git: GitRunner, base: str, commit: str, path: str) -> set[str]:
    shown = git(base, "cat-file", "blob", f"{commit}:{path}")
    if shown.returncode != 0:
        raise StaleOverrideError(f"cannot read remote SNAPSHOT {path}: {shown.stderr}")
    try:
        sources = json.loads(shown.stdout).get("data", {}).get("source_event_uuids", [])
    except (json.JSONDecodeError, AttributeError) as err:
        raise StaleOverrideError(f"remote SNAPSHOT {path} is unreadable: {err}") from err
    return {s for s in sources if isinstance(s, str)}


def _events(paths: list[str]) -> list[_Event]:
    events: list[_Event] = []
    for path in paths:
        ticket_dir, _, name = path.rpartition("/")
        if not ticket_dir or "/" in ticket_dir or ticket_dir.startswith("."):
            continue
        match = _EVENT_RE.match(name)
        if match is None:
            continue
        events.append(
            _Event(
                path=path,
                ticket_dir=ticket_dir,
                uuid=match["uuid"].lower(),
                ts=int(match["ts"]),
                event_type=match["type"],
                retired=match["retired"] is not None,
            )
        )
    return events


def _tree_paths(git: GitRunner, base: str, commit: str) -> list[str]:
    listed = _required(git, base, "ls-tree", "-r", "--name-only", commit)
    return [line for line in listed.splitlines() if line]


def _blob_ids(git: GitRunner, base: str, head: str, events: list[_Event]) -> dict[str, str]:
    """Map each event's live path to its blob id in ``head`` (captured before the reset)."""
    return {
        event.live_path: _required(git, base, "rev-parse", f"{head}:{event.path}").strip()
        for event in events
    }


def _replay(git: GitRunner, base: str, blobs: dict[str, str]) -> None:
    """Stage the original blobs at their live paths, check them out, and commit once.

    Staging by blob id keeps each replayed event byte-identical to the one written.
    """
    for live_path, blob in sorted(blobs.items()):
        _required(git, base, "update-index", "--add", "--cacheinfo", f"100644,{blob},{live_path}")
    _required(git, base, "checkout", "--", *sorted(blobs))
    _required(
        git,
        base,
        "commit",
        "--quiet",
        "--no-verify",
        "-m",
        f"rebar: replay {len(blobs)} unpushed event(s) onto reclaimed baseline",
    )


def _required(git: GitRunner, base: str, *args: str) -> str:
    proc = git(base, *args)
    if proc.returncode != 0:
        raise StaleOverrideError(f"git {' '.join(args[:2])} failed: {proc.stderr}")
    return proc.stdout


def _cutoff_ns(now: datetime | None, horizon_days: int) -> int:
    moment = now if now is not None else datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return int((moment - timedelta(days=horizon_days)).timestamp() * 1_000_000_000)


def _stamp(now: datetime | None) -> str:
    moment = now if now is not None else datetime.now(UTC)
    return moment.astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")


def _paths(events: list[_Event]) -> tuple[str, ...]:
    return tuple(sorted(event.path for event in events))
