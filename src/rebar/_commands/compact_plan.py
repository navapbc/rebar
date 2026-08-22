"""ONE compaction PLANNER — the derivations the normal fold, the fsck rebuild and crash
recovery all need (story ``3436-71db-ceff-4ac0``).

Three engines fold the same store and each re-derived the same decisions in its own code:
``compact_txn`` (the locked transaction), ``compact_rebuild`` (the fsck repair path) and
``compact_recovery`` (the crash preamble), with the sweep's selection arm in ``compact`` and
its per-close twin in ``compact_trigger``. The listing, the "is this a SNAPSHOT file" test,
the SNAPSHOT envelope, the ``*.retired`` rename-back rule and the commit step each existed in
two or three spellings — which is how the compaction family arrived as six separate bug
tickets, a fix landing in one copy while its twin stayed broken. The SNAPSHOT-exclusion rule
for ``source_event_uuids`` is the cautionary one: bug ``aea0`` had to teach it to the fold
years after the rebuild path already had it.

This module holds the DERIVATIONS only. It decides nothing about I/O policy: whether a
failure aborts, is logged, or is tolerated stays with the engine that owns that policy —
which is why ``compact_rebuild``'s forward retire loop (per-file tolerance plus a
``.snapshot-rebuild.bak`` idempotent restart) is deliberately NOT routed through the fold's
abort-and-roll-back path. It is a low-level leaf: stdlib plus ``rebar._store`` and
``rebar.reducer``, reaching ``rebar._commands._seam`` lazily so that ``compact_recovery`` can
import it without acquiring ``_seam``'s config/resolver chain.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import uuid as _uuid
from collections.abc import Iterable
from dataclasses import dataclass

from rebar._store import event_append
from rebar._store.gitutil import run_git_write
from rebar.reducer._cache import RETIRED_SUFFIX, is_active_event

logger = logging.getLogger(__name__)

#: Bridge metadata that must survive compaction untouched.
_SYNC_SUFFIX = "-SYNC.json"

#: The SNAPSHOT event-file suffix, under any ``.retired`` state.
_SNAPSHOT_SUFFIX = "-SNAPSHOT.json"


def strip_retired(name: str) -> str:
    """*name* with a trailing ``.retired`` removed."""
    return name.removesuffix(RETIRED_SUFFIX)


def is_snapshot_event_file(name: str) -> bool:
    """True when *name* is a SNAPSHOT event file, live or ``*.retired``.

    The single spelling of a test three modules used to write three ways — and the rule bug
    ``aea0`` depends on: a folded prior SNAPSHOT is absorbed STATE, never a cited source.
    """
    return strip_retired(name).endswith(_SNAPSHOT_SUFFIX)


def _is_event_file(name: str) -> bool:
    """Whether *name* is a ticket event file this planner will ever consider.

    Dotfiles (``.cache.json``, ``.snapshot-rebuild.bak``) and ``-SYNC.json`` bridge metadata
    are excluded, and the name must actually be JSON — under an optional ``.retired``.
    """
    if name.startswith(".") or name.endswith(_SYNC_SUFFIX):
        return False
    return strip_retired(name).endswith(".json")


@dataclass(frozen=True)
class Candidate:
    """One parsed event file in a ticket directory.

    ``uuid`` falls back to the file's basename when the file cannot be read or decoded, and
    ``event_type`` is then ``""`` — which is exactly how every caller's hand-rolled parse
    behaved, and why an unreadable file counts as a KNOWN type rather than being dropped as a
    forward-compat unknown.
    """

    path: str
    name: str
    uuid: str
    timestamp: int | None
    event_type: str
    is_known_type: bool

    @property
    def is_snapshot(self) -> bool:
        return is_snapshot_event_file(self.name)

    @property
    def is_active(self) -> bool:
        return is_active_event(self.name)


def list_candidates(ticket_dir: str, *, include_retired: bool = False) -> list[Candidate]:
    """Every event file in *ticket_dir*, parsed once, sorted by name.

    ``include_retired`` admits ``*.json.retired`` sources — what the fsck rebuild needs to
    recompute a SNAPSHOT from the FULL log, and what the fold must never see. Raises
    :class:`OSError` if the directory cannot be listed; callers that treat an unreadable
    ticket dir as empty keep that decision at their own call site.
    """
    from rebar.reducer import KNOWN_EVENT_TYPES

    out: list[Candidate] = []
    for name in sorted(os.listdir(ticket_dir)):
        if not _is_event_file(name):
            continue
        if not include_retired and not is_active_event(name):
            continue
        path = os.path.join(ticket_dir, name)
        try:
            with open(path, encoding="utf-8") as fh:
                event = json.load(fh)
            etype = event.get("event_type", "")
            euuid = event.get("uuid", name)
            raw_ts = event.get("timestamp")
        except (json.JSONDecodeError, OSError):
            etype, euuid, raw_ts = "", name, None
        out.append(
            Candidate(
                path=path,
                name=name,
                uuid=euuid,
                timestamp=raw_ts if isinstance(raw_ts, int) else None,
                event_type=etype,
                is_known_type=not etype or etype in KNOWN_EVENT_TYPES,
            )
        )
    return out


def source_uuids_of(candidates: Iterable[Candidate]) -> list[str]:
    """The ``source_event_uuids`` for a SNAPSHOT folding *candidates*.

    A folded prior SNAPSHOT is NOT a source (bug ``aea0`` / ``privileged-nephelite-colt``):
    its entire content IS its ``compiled_state``, which the successor absorbs, so nothing is
    lost when the file goes away — but citing it makes fsck's ``snapshot_missing_sources``
    check report a perfectly healthy ticket as damaged, and (post ``b636``) as
    un-rebuildable.
    """
    return [c.uuid for c in candidates if not c.is_snapshot]


def has_snapshot(ticket_dir: str) -> bool | None:
    """Whether *ticket_dir* holds a live ``-SNAPSHOT.json``; ``None`` if it cannot be read.

    The ``None`` is deliberate: the sweep treats an unreadable dir as already-snapshotted (it
    must not select a ticket whose fold would fail anyway) while the per-close trigger simply
    declines to fire. Returning the uncertainty lets each keep its own answer.
    """
    try:
        return any(n.endswith(_SNAPSHOT_SUFFIX) for n in os.listdir(ticket_dir))
    except OSError:
        return None


def needs_folding(foldable: int, has_snap: bool, threshold: int) -> bool:
    """The sweep's two-arm selection rule, asked once.

    * **Recurrence** — the foldable count exceeds *threshold*, whatever the snapshot state;
      without it a ticket folded once and since grown by hundreds of events would never fold
      again, and a trigger that cannot re-fire is no trigger.
    * **Backfill** — it has foldable events and no SNAPSHOT yet, so every ticket earns its
      first one regardless of size.
    """
    return foldable > threshold or (foldable > 0 and not has_snap)


def git_author() -> str:
    """The configured git author name, or ``"system"`` when git cannot answer."""
    cp = subprocess.run(["git", "config", "user.name"], capture_output=True, text=True)
    if cp.returncode != 0:
        return "system"
    return cp.stdout.strip()


def build_snapshot_event(
    tracker: str,
    ticket_dir: str,
    compiled_state: dict,
    source_uuids: list[str],
    snapshot_ts: int,
) -> tuple[dict, str]:
    """Assemble the SNAPSHOT event envelope and its destination path.

    The ONE builder for both engines. Denormalized author attribution (epic
    ``gnu-whale-ichor``) is stamped on here; neither caller has a ``repo_root`` parameter, so
    it is derived from the tracker — the derivation the two used to mirror by hand.
    """
    from pathlib import Path

    from rebar._commands import _seam

    snapshot_uuid = str(_uuid.uuid4())
    snapshot_event = {
        "event_type": "SNAPSHOT",
        "timestamp": snapshot_ts,
        "uuid": snapshot_uuid,
        "env_id": _seam.env_id(Path(tracker)),
        "author": git_author(),
        "data": {
            "compiled_state": compiled_state,
            "source_event_uuids": source_uuids,
            "compacted_at": snapshot_ts,
        },
    }
    snapshot_event.update(_seam.attribution_fields(os.path.dirname(os.path.realpath(tracker))))
    final_path = os.path.join(
        ticket_dir, event_append.event_filename(snapshot_ts, snapshot_uuid, "SNAPSHOT")
    )
    return snapshot_event, final_path


def restore_retired(originals: Iterable[str]) -> bool:
    """Rename every ``<original>.retired`` back to ``<original>``; True if ALL of them made it.

    The ``*.retired`` rename-back rule, owned once. Idempotent by construction — a source
    whose original is already present is left alone — so it is safe on a partially-reverted
    tree, which is what both the in-process rollback and the cross-process crash recovery
    hand it.

    The boolean is load-bearing, not decoration: a FALSE means at least one source is stuck
    as ``*.retired`` with its folded effect living ONLY in the (still uncommitted) SNAPSHOT,
    and the caller must then RETAIN that SNAPSHOT rather than remove it — see
    :func:`discard_uncommitted_snapshot`.
    """
    clean = True
    for original in originals:
        retired = original + RETIRED_SUFFIX
        if not os.path.exists(retired) or os.path.exists(original):
            continue
        try:
            os.rename(retired, original)
        except OSError:
            clean = False
            logger.warning("compact: could not restore %s from %s", original, retired)
    return clean


def discard_uncommitted_snapshot(path: str) -> None:
    """Remove an uncommitted SNAPSHOT after a CLEAN restore. Call ONLY when
    :func:`restore_retired` returned True — see its docstring for why."""
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning("compact: could not remove uncommitted SNAPSHOT %s", path)


# raw-git-ok: store-maintenance planner, seam-internal
def _git(tracker: str, *args: str):
    return run_git_write(tracker, *args, check=False)


def unstage_ticket_dir(tracker: str, ticket_id: str) -> None:
    """Unstage an aborted fold's own index entries: a death between ``git add`` and ``git
    commit`` leaves a dirty INDEX that aborts the union merge exactly as dirty files do."""
    _git(tracker, "reset", "-q", "--", f"{ticket_id}/")


def commit_ticket_dir(tracker: str, ticket_id: str, message: str) -> tuple[bool, str]:
    """Stage and commit one ticket directory. Returns ``(ok, stderr)``.

    Nothing staged is success with nothing committed. The caller owns the REACTION: the fold
    turns a failure into an operator-facing stderr line and a non-zero exit, while the fsck
    rebuild ignores it (the rebuild already succeeded on disk and the next store write carries
    it). ``stderr`` is handed back rather than logged here because the seam's lock-exhaustion
    guidance rides in it (bug ``9305``).
    """
    add = _git(tracker, "add", "-A", f"{ticket_id}/")
    if add.returncode != 0:
        return False, add.stderr
    if _git(tracker, "diff", "--cached", "--quiet").returncode == 0:
        return True, ""
    commit = _git(tracker, "commit", "-q", "--no-verify", "-m", message)
    if commit.returncode != 0:
        return False, commit.stderr
    return True, ""


# ── construct-uniqueness guard ───────────────────────────────────────────────────────
#: The escape marker for a legitimate second site. A reason is MANDATORY — a bare marker
#: would let the exception hide, so it is a violation in its own right (the rule
#: ``scripts/check_raw_git_writes.py`` enforces for ``# raw-git-ok:``).
_COMPACT_PLAN_OK_RE = re.compile(r"#\s*compact-plan-ok:(.*)$")

#: A SNAPSHOT-envelope key in dict-KEY position — the shape that BUILDS an envelope. The
#: read form (``data.get("compacted_at")`` in the reducer, ``.get("source_event_uuids")`` in
#: fsck) is deliberately not matched: consuming the envelope is not a second builder.
_ENVELOPE_KEY_RE = re.compile(r"""["'](?:source_event_uuids|compacted_at)["']\s*:""")

#: The rename-BACK: ``os.rename`` with ``retired`` in FIRST-argument position. The forward
#: retire (``os.rename(fp, retired)``) stays with each engine, which owns its own failure
#: policy; only the restore rule is consolidated here.
_RESTORE_RE = re.compile(r"\bos\.rename\(\s*retired\b")


def _offending_line(line: str) -> str | None:
    """Why *line* is an unsanctioned second copy of a construct this module owns, else
    ``None``.

    Split out from the tree scan in ``tests/unit/commands/test_compact_plan.py`` so the guard
    can be proven to FLAG, not merely to pass: a scan that only ever reports "no offender
    exists today" reports exactly the same thing when its matcher is broken.
    """
    if _ENVELOPE_KEY_RE.search(line):
        offence = "SNAPSHOT-envelope builder outside rebar._commands.compact_plan"
    elif _RESTORE_RE.search(line):
        offence = "'*.retired' rename-back outside rebar._commands.compact_plan"
    else:
        return None
    marker = _COMPACT_PLAN_OK_RE.search(line)
    if marker is None:
        return offence
    if marker.group(1).strip():
        return None
    return "compact-plan-ok marker requires a reason"


def offending_lines(text: str) -> list[str]:
    """Every ``"<lineno>: <why>"`` offence in *text* (a whole source file)."""
    numbered = enumerate(text.splitlines(), start=1)
    return [f"{n}: {why}" for n, line in numbered if (why := _offending_line(line)) is not None]
