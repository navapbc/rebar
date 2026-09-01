"""Ticket-directory layout helpers.

The public resolver still deals in separator-free ticket ids. This module is the single
place that maps those ids to store-relative paths.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from rebar._store import fsutil

if TYPE_CHECKING:
    from rebar._store.ensures import EnsureOutcome

SHARDED_LAYOUT_CAPABILITY = "sharded-ticket-layout"
TICKET_LAYOUT_ENSURE_ID = "ticket-layout-shards"
_HEX = frozenset("0123456789abcdef")
_EVENT_SUFFIXES = (".json", ".json.retired")


@dataclass(frozen=True)
class TicketDir:
    ticket_id: str
    path: str
    relpath: str
    sharded: bool


def shard_for_ticket_id(ticket_id: str) -> str:
    return hashlib.sha256(ticket_id.encode("utf-8")).hexdigest()[:2]


def is_shard_name(name: str) -> bool:
    return len(name) == 2 and all(ch in _HEX for ch in name)


def ticket_relpath(ticket_id: str) -> str:
    return os.path.join(shard_for_ticket_id(ticket_id), ticket_id)


def flat_ticket_dir(tracker: str | os.PathLike[str], ticket_id: str) -> str:
    return os.path.join(os.fspath(tracker), ticket_id)


def sharded_ticket_dir(tracker: str | os.PathLike[str], ticket_id: str) -> str:
    return os.path.join(os.fspath(tracker), ticket_relpath(ticket_id))


def existing_ticket_dir(tracker: str | os.PathLike[str], ticket_id: str) -> str | None:
    flat = flat_ticket_dir(tracker, ticket_id)
    sharded = sharded_ticket_dir(tracker, ticket_id)
    if os.path.isdir(flat) and (is_ticket_dir(flat) or not os.path.isdir(sharded)):
        return flat
    if os.path.isdir(sharded):
        return sharded
    return None


def ticket_dir(tracker: str | os.PathLike[str], ticket_id: str) -> str:
    return existing_ticket_dir(tracker, ticket_id) or sharded_ticket_dir(tracker, ticket_id)


def ticket_dir_relpath(tracker: str | os.PathLike[str], ticket_id: str) -> str:
    existing = existing_ticket_dir(tracker, ticket_id)
    if existing is not None:
        return os.path.relpath(existing, os.fspath(tracker))
    return ticket_relpath(ticket_id)


def ensure_shard_parent(tracker: str | os.PathLike[str], ticket_id: str) -> None:
    os.makedirs(os.path.join(os.fspath(tracker), shard_for_ticket_id(ticket_id)), exist_ok=True)


def tracker_root_from_ticket_dir(ticket_dir_path: str | os.PathLike[str]) -> str:
    path = Path(ticket_dir_path)
    parent = path.parent
    if parent.name == shard_for_ticket_id(path.name):
        return str(parent.parent)
    return str(parent)


def _event_file_names(path: Path) -> list[str]:
    try:
        return [
            child.name
            for child in path.iterdir()
            if child.is_file() and child.name.endswith(_EVENT_SUFFIXES)
        ]
    except OSError:
        return []


def is_ticket_dir(path: str | os.PathLike[str]) -> bool:
    return bool(_event_file_names(Path(path)))


def is_store_data_dir(path: str | os.PathLike[str]) -> bool:
    p = Path(path)
    if is_ticket_dir(p):
        return True
    if not p.is_dir() or not is_shard_name(p.name):
        return False
    try:
        children = [child for child in p.iterdir() if not child.name.startswith(".")]
    except OSError:
        return False
    return all(child.is_dir() and is_ticket_dir(child) for child in children)


def iter_ticket_dirs(tracker: str | os.PathLike[str]) -> list[TicketDir]:
    root = Path(tracker)
    found: dict[str, TicketDir] = {}
    try:
        entries = sorted(root.iterdir(), key=lambda p: p.name)
    except OSError:
        return []
    for entry in entries:
        if entry.name.startswith(".") or not entry.is_dir():
            continue
        if is_ticket_dir(entry):
            found.setdefault(
                entry.name,
                TicketDir(entry.name, str(entry), entry.name, False),
            )
            continue
        if not is_shard_name(entry.name):
            continue
        try:
            children = sorted(entry.iterdir(), key=lambda p: p.name)
        except OSError:
            continue
        for child in children:
            if child.name.startswith(".") or not child.is_dir() or not is_ticket_dir(child):
                continue
            found.setdefault(
                child.name,
                TicketDir(child.name, str(child), os.path.join(entry.name, child.name), True),
            )
    return [found[name] for name in sorted(found)]


def iter_ticket_ids(tracker: str | os.PathLike[str]) -> list[str]:
    return [entry.ticket_id for entry in iter_ticket_dirs(tracker)]


def pinned_tree_ticket_id(relpath: str) -> str | None:
    parts = relpath.split("/")
    if len(parts) >= 2 and is_shard_name(parts[0]):
        return parts[1]
    if parts:
        return parts[0]
    return None


def _read_compat(path: Path) -> dict[str, object]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {"format_version": 1, "required_capabilities": []}


def _stamp_capability(tracker: str) -> bool:
    from rebar._store.compat import COMPAT_FILENAME, CURRENT_FORMAT_VERSION

    path = Path(tracker) / COMPAT_FILENAME
    record = _read_compat(path)
    if not isinstance(record.get("format_version"), int):
        record["format_version"] = CURRENT_FORMAT_VERSION
    caps = record.get("required_capabilities")
    if not isinstance(caps, list):
        caps = []
    clean = [cap for cap in caps if isinstance(cap, str)]
    if SHARDED_LAYOUT_CAPABILITY in clean:
        return False
    record["required_capabilities"] = sorted({*clean, SHARDED_LAYOUT_CAPABILITY})
    body = json.dumps(record, indent=2, sort_keys=True) + "\n"
    fsutil.atomic_write(str(path), body)
    return True


def _flat_move_plan(tracker: str) -> list[tuple[str, str]]:
    moves: list[tuple[str, str]] = []
    root = Path(tracker)
    try:
        entries = sorted(root.iterdir(), key=lambda p: p.name)
    except OSError:
        return moves
    for entry_path in entries:
        if (
            entry_path.name.startswith(".")
            or not entry_path.is_dir()
            or not is_ticket_dir(entry_path)
        ):
            continue
        target = sharded_ticket_dir(tracker, entry_path.name)
        if os.path.abspath(str(entry_path)) == os.path.abspath(target):
            continue
        if os.path.exists(target):
            raise RuntimeError(f"ticket layout migration collision for {entry_path.name}: {target}")
        moves.append((str(entry_path), target))
    return moves


def _is_ticket_payload_relpath(relpath: str) -> bool:
    name = os.path.basename(relpath)
    return name == ".tombstone.json" or name.endswith(_EVENT_SUFFIXES)


def _tracked_ticket_roots(tracker: str) -> set[str]:
    from rebar._store.gitutil import run_git_write

    ls = run_git_write(tracker, "ls-files", "-z", check=False)
    if ls.returncode != 0:
        return set()
    roots: set[str] = set()
    for relpath in ls.stdout.split("\0"):
        if not relpath or not _is_ticket_payload_relpath(relpath):
            continue
        parts = relpath.split("/")
        if not parts or parts[0].startswith("."):
            continue
        if len(parts) >= 3 and is_shard_name(parts[0]):
            roots.add(os.path.join(parts[0], parts[1]))
        elif len(parts) >= 2:
            roots.add(parts[0])
    return roots


def _migration_stage_relpaths(tracker: str, moves: list[tuple[str, str]]) -> list[str]:
    relpaths = {".store-compat.json"}
    relpaths.update(os.path.relpath(path, tracker) for pair in moves for path in pair)
    relpaths.update(_tracked_ticket_roots(tracker))
    for entry in iter_ticket_dirs(tracker):
        relpaths.add(entry.relpath)
    return sorted(relpaths)


def migrate_flat_ticket_dirs_unit(tracker: str) -> EnsureOutcome:
    from rebar._store.ensures import EnsureOutcome
    from rebar._store.gitutil import run_git_write

    tracker = os.fspath(tracker)
    moves = _flat_move_plan(tracker)
    changed_compat = _stamp_capability(tracker)
    if not moves and not changed_compat:
        return EnsureOutcome(TICKET_LAYOUT_ENSURE_ID, "ok", "ticket layout already sharded")

    moved: list[tuple[str, str]] = []
    try:
        for source, target in moves:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.move(source, target)
            moved.append((source, target))
    except OSError:
        for source, target in reversed(moved):
            if os.path.exists(target) and not os.path.exists(source):
                os.makedirs(os.path.dirname(source), exist_ok=True)
                shutil.move(target, source)
        raise

    relpaths = _migration_stage_relpaths(tracker, moves)
    add = run_git_write(  # raw-git-ok: ensure-registry store-maintenance seam
        tracker, "add", "-A", "--", *relpaths, check=False
    )
    if add.returncode != 0:
        raise RuntimeError((add.stderr or add.stdout).strip() or "git add failed")
    diff = run_git_write(  # raw-git-ok: scoped staged-tree check in ensure seam
        tracker, "diff", "--cached", "--quiet", "--", *relpaths, check=False
    )
    if diff.returncode == 0:
        return EnsureOutcome(TICKET_LAYOUT_ENSURE_ID, "ok", "ticket layout already sharded")
    commit = run_git_write(  # raw-git-ok: ensure-registry store-maintenance seam
        tracker,
        "commit",
        "-q",
        "--no-verify",
        "-m",
        "chore: migrate ticket directories to sharded layout",
        check=False,
    )
    if commit.returncode != 0:
        raise RuntimeError((commit.stderr or commit.stdout).strip() or "git commit failed")
    return EnsureOutcome(
        TICKET_LAYOUT_ENSURE_ID,
        "changed",
        f"migrated {len(moves)} ticket dir(s) to sharded layout",
    )
