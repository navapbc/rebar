"""The reconciler's repo-local scratch layout under ``<repo>/.rebar/bridge/``, and its retention.

The reconciler keeps per-pass diagnostics and a little cross-pass bookkeeping on local disk,
next to the checkout it runs from. This module is the single owner of that layout: every
writer composes its path through the helpers below, and :func:`prune_bridge_scratch` bounds
the per-pass artifacts at the end of each writing pass so the directory cannot grow without
limit.

Layout, all under ``<repo_root>/.rebar/bridge/``:

* ``snapshots/<pass_id>.json`` and ``snapshots/<pass_id>.manifest.json`` — the pass's fetched
  snapshot and applier manifest. The newest :data:`RETAIN_SNAPSHOT_PASSES` passes are kept.
* ``sync-log-<pass_id>.jsonl`` — the pass's structured audit log. The newest
  :data:`RETAIN_SYNC_LOGS` are kept.
* ``bridge_alerts/YYYY-MM-DD.jsonl`` — the daily alert log. The newest
  :data:`RETAIN_ALERT_DAYS` files are kept.
* ``mapping.json`` and ``comment_id_divergence.json`` — cross-pass state, never pruned.

``.gitignore``'s ``.rebar/*`` already ignores the whole tree, so it is never committed. It is
distinct from the tracker-side ``<tracker>/.bridge_state/`` (bindings, projects, previous
snapshot, last pass), which is committed state and is neither named nor touched here.

Pass ids are UTC ``%Y-%m-%dT%H-%M-%S`` timestamps and alert files are named by UTC date, so
name order is age order and retention needs no clock or file metadata. Retention mirrors
``rebar.llm.completion_sidecar.prune``: keep the newest N by name, best-effort, and never
fail the caller.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from pathlib import Path

logger = logging.getLogger(__name__)

BRIDGE_SCRATCH_SUBDIR = Path(".rebar") / "bridge"

# Retention bounds for prune_bridge_scratch's defaults.
RETAIN_SNAPSHOT_PASSES = 5
RETAIN_SYNC_LOGS = 5
RETAIN_ALERT_DAYS = 30

_SNAPSHOTS_SUBDIR = "snapshots"
_ALERTS_SUBDIR = "bridge_alerts"
_SYNC_LOG_PREFIX = "sync-log-"
_SNAPSHOT_SUFFIXES = (".json", ".json.tmp")


def bridge_scratch_dir(repo_root: Path) -> Path:
    """Return ``<repo_root>/.rebar/bridge``, the root of the reconciler's local scratch."""
    return Path(repo_root) / BRIDGE_SCRATCH_SUBDIR


def snapshots_dir(repo_root: Path) -> Path:
    """Return the per-pass snapshot and manifest directory."""
    return bridge_scratch_dir(repo_root) / _SNAPSHOTS_SUBDIR


def alerts_dir(repo_root: Path) -> Path:
    """Return the daily ``bridge_alerts`` JSONL directory."""
    return bridge_scratch_dir(repo_root) / _ALERTS_SUBDIR


def sync_log_path(repo_root: Path, pass_id: str) -> Path:
    """Return the structured sync-log file for ``pass_id``."""
    return bridge_scratch_dir(repo_root) / f"{_SYNC_LOG_PREFIX}{pass_id}.jsonl"


def mapping_path(repo_root: Path) -> Path:
    """Return the cross-pass local-id/Jira-key ``mapping.json`` (never pruned)."""
    return bridge_scratch_dir(repo_root) / "mapping.json"


def divergence_state_path(repo_root: Path) -> Path:
    """Return the cross-pass comment-id divergence counter file (never pruned)."""
    return bridge_scratch_dir(repo_root) / "comment_id_divergence.json"


def _files(directory: Path) -> list[Path]:
    """Return the regular, non-hidden files directly under ``directory`` (none when absent)."""
    if not directory.is_dir():
        return []
    return [p for p in directory.iterdir() if p.is_file() and not p.name.startswith(".")]


def _remove(paths: Iterable[Path]) -> int:
    """Unlink each path, skipping any that fail, and return how many were removed."""
    removed = 0
    for path in paths:
        try:
            path.unlink()
        except OSError:
            logger.warning("bridge scratch prune could not remove %s; continuing", path)
            continue
        removed += 1
    return removed


def _oldest_beyond(names: Iterable[str], keep: int) -> list[str]:
    """Return the names that fall outside the newest ``keep`` in name order."""
    ordered = sorted(names)
    return ordered[: max(0, len(ordered) - keep)]


def _prune_snapshots(directory: Path, keep: int) -> int:
    """Remove every file of the passes older than the newest ``keep``.

    A pass's files share its id as the name prefix up to the first dot: ``<pass>.json``,
    ``<pass>.manifest.json``, and a stray ``<pass>.<random>.json.tmp`` from an interrupted
    manifest write.
    """
    by_pass: dict[str, list[Path]] = {}
    for path in _files(directory):
        if path.name.endswith(_SNAPSHOT_SUFFIXES):
            by_pass.setdefault(path.name.split(".", 1)[0], []).append(path)
    return _remove(p for pass_id in _oldest_beyond(by_pass, keep) for p in by_pass[pass_id])


def _prune_by_name(paths: list[Path], keep: int) -> int:
    by_name = {p.name: p for p in paths}
    return _remove(by_name[name] for name in _oldest_beyond(by_name, keep))


def _prune_sync_logs(directory: Path, keep: int) -> int:
    logs = [
        p
        for p in _files(directory)
        if p.name.startswith(_SYNC_LOG_PREFIX) and p.name.endswith(".jsonl")
    ]
    return _prune_by_name(logs, keep)


def _prune_alerts(directory: Path, keep: int) -> int:
    return _prune_by_name([p for p in _files(directory) if p.name.endswith(".jsonl")], keep)


def prune_bridge_scratch(
    repo_root: Path,
    *,
    keep_passes: int = RETAIN_SNAPSHOT_PASSES,
    keep_logs: int = RETAIN_SYNC_LOGS,
    keep_alert_days: int = RETAIN_ALERT_DAYS,
) -> dict[str, int]:
    """Bound the per-pass scratch under ``<repo_root>/.rebar/bridge/``.

    Keeps the newest ``keep_passes`` passes of snapshots and manifests, the newest
    ``keep_logs`` sync logs, and the newest ``keep_alert_days`` daily alert files, and returns
    the number of files removed per category (``snapshots``, ``sync_logs``, ``alerts``).
    ``mapping.json`` and ``comment_id_divergence.json`` are cross-pass state and are never
    touched.

    Best-effort: a category whose directory cannot be listed logs a warning and counts zero,
    and a file that cannot be removed is skipped. A failed prune never fails the pass.
    """
    root = Path(repo_root)
    plan: tuple[tuple[str, Callable[[Path, int], int], Path, int], ...] = (
        ("snapshots", _prune_snapshots, snapshots_dir(root), keep_passes),
        ("sync_logs", _prune_sync_logs, bridge_scratch_dir(root), keep_logs),
        ("alerts", _prune_alerts, alerts_dir(root), keep_alert_days),
    )
    removed: dict[str, int] = {}
    for category, prune, directory, keep in plan:
        try:
            removed[category] = prune(directory, keep)
        except Exception:  # retention is best-effort; a failed prune never fails the pass
            logger.warning(
                "bridge scratch prune of %s failed; continuing", directory, exc_info=True
            )
            removed[category] = 0
    return removed
