"""Read-only, layered tracker-footprint measurement.

The report deliberately keeps Git packing, checkout payload, filesystem allocation,
and whole-clone residence separate.  Measurement never initializes, fetches, or
otherwise changes the tracker.  The only network-capable operation is the explicit
``measure_fresh_clone`` path, whose temporary directory is command-owned and scoped by
``TemporaryDirectory``.
"""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from os import PathLike
from pathlib import Path
from typing import Literal

from rebar._store.paths import StorePaths

_lstat = os.lstat

_ALLOCATION_UNAVAILABLE = "filesystem stat does not expose st_blocks"

DEFINITIONS: dict[str, str] = {
    "logical_bytes": (
        "Sum of lstat().st_size for every non-directory pathname; hard links are "
        "counted once per pathname."
    ),
    "allocated_bytes": (
        "Sum of st_blocks * 512, charged once per (st_dev, st_ino); unavailable "
        "when filesystem stat does not expose st_blocks."
    ),
    "allocation_overhead_bytes": (
        "Allocated bytes minus logical bytes; the result may be negative."
    ),
    "pack": (
        "Logical bytes of .pack files in the primary common Git object database; "
        "non-exclusive when the checkout borrows objects from an alternate object "
        "database, in which case the pack layer reports complete=false and must not "
        "be read as the whole object store."
    ),
    "whole_clone": (
        "The unique union of checkout and Git-directory pathnames, with allocation "
        "inode-deduplicated across both roots."
    ),
}


class FootprintError(RuntimeError):
    """A tracker footprint could not be measured."""


@dataclass
class _Totals:
    """Logical and physical totals for one pathname set."""

    logical_bytes: int = 0
    file_count: int = 0
    allocated_bytes: int = 0
    allocation_available: bool = True
    seen_inodes: set[tuple[int, int]] = field(default_factory=set)

    def add(self, info: os.stat_result) -> None:
        self.logical_bytes += info.st_size
        self.file_count += 1
        blocks = getattr(info, "st_blocks", None)
        if blocks is None:
            self.allocation_available = False
            return
        inode = (info.st_dev, info.st_ino)
        if inode not in self.seen_inodes:
            self.seen_inodes.add(inode)
            self.allocated_bytes += int(blocks) * 512


def _availability(value: int | None) -> dict[str, object]:
    if value is None:
        return {"unavailable": {"reason": _ALLOCATION_UNAVAILABLE}}
    return {"value": value}


def _layer(totals: _Totals) -> dict[str, object]:
    allocated = totals.allocated_bytes if totals.allocation_available else None
    overhead = allocated - totals.logical_bytes if allocated is not None else None
    return {
        "logical_bytes": totals.logical_bytes,
        "file_count": totals.file_count,
        "allocated_bytes": _availability(allocated),
        "allocation_overhead_bytes": _availability(overhead),
    }


def _walk_files(
    root: Path, *, exclude_root_names: frozenset[str] = frozenset()
) -> Iterator[os.stat_result]:
    """Yield lstat results for non-directory pathnames below ``root``.

    Directory symlinks are yielded rather than followed.  Sorting is not required for
    arithmetic, but makes the first surfaced filesystem failure deterministic.
    """

    pending = [root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            ordered = sorted(entries, key=lambda entry: entry.name, reverse=True)
        for entry in ordered:
            if directory == root and entry.name in exclude_root_names:
                continue
            path = Path(entry.path)
            info = _lstat(path)
            if stat.S_ISDIR(info.st_mode):
                pending.append(path)
            else:
                yield info


def _scan(
    root: Path,
    *totals: _Totals,
    exclude_root_names: frozenset[str] = frozenset(),
) -> None:
    for info in _walk_files(root, exclude_root_names=exclude_root_names):
        for total in totals:
            total.add(info)


def _normalized_root(path: str | PathLike[str]) -> Path:
    return Path(os.path.realpath(os.path.abspath(os.fspath(path))))


def _unique_roots(*paths: str | PathLike[str]) -> tuple[Path, ...]:
    """Return a deterministic minimal union of roots.

    A linked worktree's git dir normally lives below its common dir.  Keeping only
    the outer root prevents every pathname below the git dir from being counted twice.
    """

    candidates = sorted(
        {_normalized_root(path) for path in paths}, key=lambda p: (len(p.parts), str(p))
    )
    selected: list[Path] = []
    for candidate in candidates:
        if any(candidate == root or candidate.is_relative_to(root) for root in selected):
            continue
        selected.append(candidate)
    return tuple(selected)


def _git(  # raw-git-ok: read-only footprint probes, variable subcommand
    repo: Path, *args: str
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise FootprintError("git is unavailable for tracker footprint measurement") from exc


def _source(repo: Path, *, remote: str, branch: str) -> dict[str, str]:
    tip_result = _git(repo, "rev-parse", "--verify", "HEAD")
    if tip_result.returncode:
        raise FootprintError("cannot resolve the measured tracker tip")
    ref_result = _git(repo, "symbolic-ref", "-q", "HEAD")
    if ref_result.returncode not in (0, 1):
        raise FootprintError("cannot resolve the measured tracker ref")
    measured_ref = ref_result.stdout.strip() if ref_result.returncode == 0 else "HEAD"
    return {
        "remote": remote,
        "branch": branch,
        "requested_ref": f"{remote}/{branch}",
        "measured_ref": measured_ref,
        "tip": tip_result.stdout.strip(),
    }


def _has_alternates(common_dir: Path) -> bool:
    alternates = common_dir / "objects" / "info" / "alternates"
    try:
        return bool(alternates.read_text(encoding="utf-8").strip())
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise FootprintError("cannot inspect the tracker object database") from exc


def _object_database(paths: StorePaths) -> tuple[str, list[str], Path, tuple[Path, ...]]:
    git_dir = _normalized_root(paths.git_dir)
    common_dir = _normalized_root(paths.git_common_dir)
    reasons: list[str] = []
    if git_dir != common_dir:
        reasons.append("linked-worktree")
    if _has_alternates(common_dir):
        reasons.append("alternates")
    scope = "shared" if reasons else "standalone"
    return scope, reasons, common_dir, _unique_roots(git_dir, common_dir)


def _pack_layer(common_dir: Path, *, scope: str, complete: bool) -> dict[str, object]:
    pack_dir = common_dir / "objects" / "pack"
    logical_bytes = 0
    file_count = 0
    try:
        entries = (
            sorted(pack_dir.iterdir(), key=lambda path: path.name) if pack_dir.is_dir() else []
        )
        for path in entries:
            info = _lstat(path)
            if path.name.endswith(".pack") and not stat.S_ISDIR(info.st_mode):
                logical_bytes += info.st_size
                file_count += 1
    except OSError as exc:
        raise FootprintError("cannot inspect tracker pack files") from exc
    return {
        "logical_bytes": logical_bytes,
        "file_count": file_count,
        "scope": scope,
        "complete": complete,
    }


def measure_tracker(
    tracker: str | PathLike[str],
    *,
    remote: str,
    branch: str,
    mode: Literal["mounted", "fresh-clone"] = "mounted",
) -> dict[str, object]:
    """Measure one materialized tracker without changing it."""

    if mode not in ("mounted", "fresh-clone"):
        raise ValueError(f"unsupported tracker footprint mode: {mode}")
    tracker_root = _normalized_root(tracker)
    paths = StorePaths(tracker_root)
    try:
        scope, reasons, common_dir, git_roots = _object_database(paths)
        pack_complete = "alternates" not in reasons
        checkout = _Totals()
        git_directory = _Totals()
        whole = _Totals()
        _scan(tracker_root, checkout, whole, exclude_root_names=frozenset({".git"}))
        for root in git_roots:
            _scan(root, git_directory, whole)
        source = _source(tracker_root, remote=remote, branch=branch)
    except FootprintError:
        raise
    except OSError as exc:
        raise FootprintError("cannot read the configured tracker footprint") from exc

    return {
        "mode": mode,
        "source": source,
        "object_database": {"scope": scope, "shared_reasons": reasons},
        "layers": {
            "pack": _pack_layer(common_dir, scope=scope, complete=pack_complete),
            "checkout": _layer(checkout),
            "git_directory": _layer(git_directory),
            "whole_clone": {**_layer(whole), "scope": scope},
        },
        "definitions": dict(DEFINITIONS),
    }


def _remote_url(repo_root: Path, *, remote: str, branch: str) -> str:
    try:
        result = _git(repo_root, "remote", "get-url", remote)
    except FootprintError as exc:
        raise FootprintError(f"cannot resolve tracker source {remote}/{branch}") from exc
    url = result.stdout.strip()
    if result.returncode or not url:
        raise FootprintError(f"cannot resolve tracker source {remote}/{branch}")
    return url


def measure_fresh_clone(repo_root: str | PathLike[str]) -> dict[str, object]:
    """Clone the configured tickets ref temporarily and measure that clone."""

    from rebar import config

    root = Path(repo_root)
    remote = config.tickets_remote(root)
    branch = config.tickets_branch(root)
    remote_url = _remote_url(root, remote=remote, branch=branch)
    with tempfile.TemporaryDirectory(prefix="rebar-tracker-footprint-") as temporary:
        clone = Path(temporary) / "tracker"
        try:
            result = subprocess.run(
                [
                    "git",
                    "clone",
                    "--single-branch",
                    "--branch",
                    branch,
                    "--no-tags",
                    "--no-local",
                    remote_url,
                    str(clone),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:
            raise FootprintError(f"cannot clone tracker source {remote}/{branch}") from exc
        if result.returncode:
            raise FootprintError(f"cannot clone tracker source {remote}/{branch}")
        try:
            return measure_tracker(clone, remote=remote, branch=branch, mode="fresh-clone")
        except FootprintError as exc:
            raise FootprintError(f"cannot measure tracker source {remote}/{branch}") from exc
