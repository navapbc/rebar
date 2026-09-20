"""Detect a stalled global-build updater from local disk alone (ticket ae97-a37b).

A LaunchAgent keeps this host's global ``rebar`` aligned to ``origin/main``. On
2026-09-03 it had failed 122 consecutive times and the build had drifted 201 commits,
and the alert built to announce that could not deliver -- the SNS topic sits in a
different account with no resource policy permitting the publish. The streak reached 122
against a threshold of 3 with zero operator signal.

The lesson is not about SNS. Any detector whose only path to an operator is a remote sink
is silent in exactly the conditions worth detecting. So this one reads local disk and
reports through ``rebar doctor``, which the operator already runs.

Two signals, deliberately independent, because a stall that silences one can leave the
other speaking:

``reject-streak``
    The updater's own consecutive-rejection counter, at or above its alert threshold.

``build-stale``
    How far the build behind the updater's ``current`` pointer trails ``origin/main`` in
    the source repository the updater state names.

Every finding is ADVISORY. Nothing here describes the ticket store -- it is read from the
operator's HOME -- so folding it into ``doctor``'s exit code would make a store-health
gate depend on whichever updater happens to sit on the box running it. That is the same
ground on which ``doctor`` already excludes its MCP-client findings.

ABSENT IS NOT BROKEN. Most boxes run no such updater; that yields exactly one
``unavailable`` finding and nothing else.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Where the updater keeps its state, relative to the operator's home.
STATE_RELPATH = Path(".local/state/rebar-dev")

#: The updater's own consecutive-rejection counter.
STREAK_FILENAME = "reject-streak"

#: The published build's sha, written by the updater when a candidate is accepted.
CURRENT_SHA_RELPATH = Path("current/sha")

#: The source repository path the updater tracks.
SOURCE_REPO_RELPATH = Path("source-repo")

#: The ref the updater tracks. A build is "stale" only relative to what it chases.
TRACKED_REF = "origin/main"

#: Ceiling on every git probe. A diagnostic must never block on a wedged git.
GIT_PROBE_TIMEOUT_S = 10

#: Consecutive rejections before the streak is a finding. Mirrors the updater's own
#: ``REJECT_STREAK_ALERT``, so the two agree on what "too many" means.
REJECT_STREAK_ALERT = 3

#: Commits behind :data:`TRACKED_REF` before the build is a finding.
#:
#: An hourly updater sits at 0-2 commits behind when healthy, while the two recorded
#: incidents reached roughly 48 and 195. 25 therefore falls below the smaller incident
#: and an order of magnitude above healthy.
MAX_COMMITS_BEHIND = 25

# Both thresholds are module constants rather than config keys or env vars on purpose:
# the mechanism-delta ratchet counts those kinds, and this detector needs no new
# configuration surface. Callers that need a different number pass it to the scan.

SIGNAL_REJECT_STREAK = "reject-streak"
SIGNAL_BUILD_STALE = "build-stale"
SIGNAL_UPDATER = "updater"

KIND_REJECT_STREAK = "reject-streak-at-threshold"
KIND_STREAK_UNREADABLE = "reject-streak-unreadable"
KIND_BUILD_STALE = "build-behind-tracked-ref"
KIND_BUILD_UNMEASURABLE = "build-sha-unmeasurable"
KIND_UPDATER_ABSENT = "updater-absent"
KIND_OK = "ok"

SEVERITY_WARNING = "warning"
SEVERITY_UNAVAILABLE = "unavailable"
SEVERITY_OK = "ok"


def scan_build_freshness(
    *,
    home: Path | None = None,
    repo_root: Path | str | None = None,
    reject_streak_alert: int = REJECT_STREAK_ALERT,
    max_commits_behind: int = MAX_COMMITS_BEHIND,
) -> list[dict[str, Any]]:
    """Report both freshness signals for the updater state under ``home``.

    ``home`` defaults to the real home directory and is injectable so the scan runs
    against a fixture tree. ``repo_root`` is accepted for caller compatibility, but the
    build-stale signal reads the source repository from updater state rather than from
    the store that invoked ``doctor``. Returns a flat list of finding dicts, each
    carrying ``signal``, ``severity``, ``kind`` and ``detail``.

    Never raises. Every fault -- an absent state directory, a file where a directory
    belongs, an unreadable counter, a sha git cannot resolve -- degrades into a finding,
    because a diagnostic that fails by raising is one more thing that can go silent.
    """
    if repo_root is not None:
        logger.debug(
            "build freshness: ignoring caller repo_root=%s; using updater state", repo_root
        )
    base = Path.home() if home is None else Path(home)
    state = base / STATE_RELPATH
    if not state.is_dir():
        return [
            _finding(
                SIGNAL_UPDATER,
                SEVERITY_UNAVAILABLE,
                KIND_UPDATER_ABSENT,
                f"no updater state at {state} — this box runs no rebar-dev updater, "
                "so neither freshness signal applies",
                path=str(state),
            )
        ]
    return [
        _scan_reject_streak(state, reject_streak_alert),
        _scan_build_stale(state, max_commits_behind),
    ]


def has_stale_build(findings: Iterable[Mapping[str, Any]]) -> bool:
    """True when any freshness signal is warning.

    ``doctor`` reports these findings but does NOT fold them into its exit code, for the
    reason given in this module's docstring. This predicate is the seam for a caller that
    DOES want to gate on updater health.
    """
    return any(f.get("severity") == SEVERITY_WARNING for f in findings)


def render_text(findings: Iterable[Mapping[str, Any]]) -> list[str]:
    """Render the freshness section as text lines (the caller prints them).

    The header is emitted even with no findings: "this check ran and found nothing" is a
    different answer from "this check did not run", and an operator chasing a silent
    updater needs to tell them apart.
    """
    lines = ["doctor: build freshness"]
    lines.extend(
        f"  {f.get('signal', '?')} [{f.get('severity', '?')}] {f.get('kind', '?')}: "
        f"{f.get('detail', '')}"
        for f in findings
    )
    return lines


def _finding(signal: str, severity: str, kind: str, detail: str, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "signal": signal,
        "severity": severity,
        "kind": kind,
        "detail": detail,
    }
    out.update(extra)
    return out


def _read_text(path: Path) -> str | None:
    """The file's contents, or ``None`` for anything that stops us reading it."""
    try:
        return path.read_text()
    except OSError:
        logger.debug("build freshness: could not read %s", path, exc_info=True)
        return None


def _scan_reject_streak(state: Path, threshold: int) -> dict[str, Any]:
    """Signal 1: the updater's own consecutive-rejection counter."""
    path = state / STREAK_FILENAME
    raw = _read_text(path)
    if raw is None:
        return _finding(
            SIGNAL_REJECT_STREAK,
            SEVERITY_UNAVAILABLE,
            KIND_STREAK_UNREADABLE,
            f"no readable rejection counter at {path}",
            path=str(path),
        )
    try:
        streak = int(raw.strip())
    except ValueError:
        # Deliberately NOT treated as zero. Reading a corrupt counter as healthy would
        # turn the exact condition this exists to catch into a clean bill of health.
        return _finding(
            SIGNAL_REJECT_STREAK,
            SEVERITY_UNAVAILABLE,
            KIND_STREAK_UNREADABLE,
            f"rejection counter at {path} is not an integer: {raw.strip()!r}",
            path=str(path),
        )
    if streak >= threshold:
        return _finding(
            SIGNAL_REJECT_STREAK,
            SEVERITY_WARNING,
            KIND_REJECT_STREAK,
            f"the updater has rejected {streak} consecutive candidates "
            f"(threshold {threshold}) — the global rebar build is not advancing; "
            f"see {state / 'update.log'} for the rejecting stage",
            streak=streak,
            threshold=threshold,
            path=str(path),
        )
    return _finding(
        SIGNAL_REJECT_STREAK,
        SEVERITY_OK,
        KIND_OK,
        f"{streak} consecutive rejection(s), below the threshold of {threshold}",
        streak=streak,
        threshold=threshold,
        path=str(path),
    )


def _scan_build_stale(state: Path, max_behind: int) -> dict[str, Any]:
    """Signal 2: how far the published build trails :data:`TRACKED_REF`."""
    path = state / CURRENT_SHA_RELPATH
    raw = _read_text(path)
    if raw is None or not raw.strip():
        return _finding(
            SIGNAL_BUILD_STALE,
            SEVERITY_UNAVAILABLE,
            KIND_BUILD_UNMEASURABLE,
            f"no readable published-build pointer at {path}",
            path=str(path),
        )
    build_sha = raw.strip()
    source_repo = _source_repo(state)
    if source_repo is None:
        return _source_repo_unavailable(build_sha, state / SOURCE_REPO_RELPATH, path)

    behind = _commits_behind(build_sha, source_repo)
    if behind is None:
        return _finding(
            SIGNAL_BUILD_STALE,
            SEVERITY_UNAVAILABLE,
            KIND_BUILD_UNMEASURABLE,
            f"cannot measure build {build_sha[:9]} against {TRACKED_REF} in "
            f"{source_repo}: the build, tracked ref, or their relationship is unavailable",
            build_sha=build_sha,
            source_repo=source_repo,
            path=str(path),
        )
    if behind > max_behind:
        return _finding(
            SIGNAL_BUILD_STALE,
            SEVERITY_WARNING,
            KIND_BUILD_STALE,
            f"the published build {build_sha[:9]} is {behind} commits behind "
            f"{TRACKED_REF} (threshold {max_behind}) — the updater is not keeping this "
            "host current",
            build_sha=build_sha,
            commits_behind=behind,
            threshold=max_behind,
            source_repo=source_repo,
            path=str(path),
        )
    return _finding(
        SIGNAL_BUILD_STALE,
        SEVERITY_OK,
        KIND_OK,
        f"the published build {build_sha[:9]} is {behind} commit(s) behind "
        f"{TRACKED_REF}, within the threshold of {max_behind}",
        build_sha=build_sha,
        commits_behind=behind,
        threshold=max_behind,
        source_repo=source_repo,
        path=str(path),
    )


def _source_repo(state: Path) -> str | None:
    path = state / SOURCE_REPO_RELPATH
    raw = _read_text(path)
    if raw is None or not raw.strip():
        return None
    source_repo = Path(raw.strip()).expanduser()
    if not source_repo.is_dir():
        logger.debug("build freshness: source repository path is unavailable: %s", source_repo)
        return None
    return str(source_repo)


def _source_repo_unavailable(build_sha: str, source_path: Path, build_path: Path) -> dict[str, Any]:
    return _finding(
        SIGNAL_BUILD_STALE,
        SEVERITY_UNAVAILABLE,
        KIND_BUILD_UNMEASURABLE,
        f"cannot measure build {build_sha[:9]} against {TRACKED_REF}: updater state does "
        f"not name an available source repository at {source_path}",
        build_sha=build_sha,
        source_repo_path=str(source_path),
        path=str(build_path),
    )


def _commits_behind(build_sha: str, repo_root: str) -> int | None:
    """Commits on :data:`TRACKED_REF` that ``build_sha`` lacks, or ``None`` if unknown.

    A build AHEAD of the tracked ref returns 0, not ``None``: a local commit past the
    tracking ref is normal on a dev box and must never read as a fault. ``None`` is
    reserved for genuinely cannot-measure -- either sha absent from the repo, no git, a
    wedged git. That distinction is the point: collapsing it into 0 would let a pointer
    at a garbage-collected commit read as healthy forever.
    """
    try:
        from rebar.llm.build_drift import resolve_commit

        build_full = resolve_commit(build_sha, repo_root)
        tracked_full = resolve_commit(TRACKED_REF, repo_root)
        if build_full is None or tracked_full is None:
            return None
    # A diagnostic probe must never raise out of doctor.
    except Exception:
        logger.debug("build freshness: drift probe failed", exc_info=True)
        return None
    if build_full == tracked_full:
        return 0

    build_is_ancestor = _is_ancestor(build_full, tracked_full, repo_root)
    if build_is_ancestor is None:
        return None
    if build_is_ancestor:
        raw_count = _git_stdout(repo_root, "rev-list", "--count", f"{build_full}..{tracked_full}")
        try:
            commits_behind = int(raw_count) if raw_count else 0
        except ValueError:
            return None
        return commits_behind if commits_behind > 0 else None

    tracked_is_ancestor = _is_ancestor(tracked_full, build_full, repo_root)
    if tracked_is_ancestor is None:
        return None
    return 0 if tracked_is_ancestor else None


def _git_stdout(repo_root: str, *args: str) -> str | None:
    try:
        out = subprocess.run(  # raw-git-ok: read-only freshness probe
            ["git", "-C", repo_root, *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=GIT_PROBE_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    value = out.stdout.strip()
    return value or None


def _is_ancestor(ancestor: str, descendant: str, repo_root: str) -> bool | None:
    try:
        out = subprocess.run(  # raw-git-ok: read-only ancestry probe
            ["git", "-C", repo_root, "merge-base", "--is-ancestor", ancestor, descendant],
            capture_output=True,
            text=True,
            check=False,
            timeout=GIT_PROBE_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode == 0:
        return True
    if out.returncode == 1:
        return False
    return None
