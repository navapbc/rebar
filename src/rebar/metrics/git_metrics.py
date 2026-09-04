"""Git and structural code-health metric derivations.

Structural metrics consume normalized analyzer results once per metrics context.
Git-history metrics remain deterministic derivations over ``git log --numstat`` (churn,
refactor-to-addition ratio) or over bounded per-revision blob reads (module-size trend,
cap-change events; ticket 21de-f9d9).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import date, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any, Literal

from rebar.metrics.analyzer import AnalyzerResult
from rebar.metrics.analyzers import jscpd_dup, lizard_complexity, scc_loc
from rebar.metrics.registry import REGISTRY, MetricSpec, Unavailable

# Watchdog on the read-only metrics git walks (bug 9305): log/numstat over a long-lived
# branch is legitimately slow and holds no store lock, so this is generous (research 5b
# rec 3). A ``TimeoutExpired`` propagates like any other failure to the per-metric fault
# isolation in ``_commands/metrics.py``, which renders the one metric unavailable.
_GIT_TIMEOUT = 300

# The single-sourced module-size cap file the CI gate reads (docs/architecture.md,
# ADR 0058). module_size_trend/cap_change_events read its historical blob per revision.
_CAP_FILE = ".github/module-size-limit.txt"

# Bound on retained module_size_trend samples (ticket 21de-f9d9): at most this many
# evenly spaced revisions, always including the first and last, regardless of how many
# qualified revisions exist in range.
_SAMPLE_SIZE = 50


# raw-git-ok: read-oriented git helper, variable subcommand
def _git(repo_root: str, *args: str) -> str:
    """Run a git subcommand in ``repo_root`` and return its stdout."""
    return subprocess.run(
        ["git", *args],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT,
    ).stdout


def _parse_date(text: str | None) -> date | None:
    """Parse an ISO date string (``YYYY-MM-DD`` or fuller ISO) to a ``date``."""
    if not text:
        return None
    text = text.strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _in_range(commit_iso: str, since: date | None, until: date | None) -> bool:
    """Inclusive date-range membership by the commit's committer date.

    Filtering is done in-process rather than via ``git log --since/--until``
    because some git builds mis-parse far-future dates (e.g. ``2100-01-01``).
    """
    cdate = datetime.fromisoformat(commit_iso.strip()).date()
    if since is not None and cdate < since:
        return False
    if until is not None and cdate > until:
        return False
    return True


def module_size_distribution(
    loc: dict[str, Any], size_cap: int | None, size_near_fraction: float
) -> dict[str, int | None]:
    """Summarize analyzer LOC, optionally classifying modules against a cap."""

    files = loc["files"]
    result: dict[str, int | None] = {
        "count": len(files),
        "near_cap_count": None,
        "over_cap_count": None,
        "max_loc": loc["max_loc"],
    }
    if size_cap is None:
        return result

    near_threshold = size_cap * (1 - size_near_fraction)
    values = files.values()
    result["near_cap_count"] = sum(near_threshold <= value <= size_cap for value in values)
    result["over_cap_count"] = sum(value > size_cap for value in values)
    return result


def oversized_module_count(
    loc: dict[str, Any], size_cap: int | None, size_near_fraction: float
) -> int | None:
    """Return the number of analyzer-reported modules over the configured cap."""

    del size_near_fraction
    if size_cap is None:
        return None
    return sum(value > size_cap for value in loc["files"].values())


def _numstat_totals(repo_root: str, since: str, until: str) -> tuple[int, int]:
    """Sum insertions/deletions from ``git log --numstat`` over a date range."""
    since_d = _parse_date(since)
    until_d = _parse_date(until)
    out = _git(
        repo_root,
        "log",
        "--numstat",
        "--format=commit %cI",
    )
    insertions = 0
    deletions = 0
    in_range = False
    for line in out.splitlines():
        if line.startswith("commit "):
            in_range = _in_range(line[len("commit ") :], since_d, until_d)
            continue
        if not in_range:
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        added, removed = parts[0], parts[1]
        if added == "-" or removed == "-":
            # Binary file — numstat reports "-"; not counted as line churn.
            continue
        insertions += int(added)
        deletions += int(removed)
    return insertions, deletions


def churn(repo_root: str, since: str, until: str) -> dict[str, int]:
    """Return ``{"insertions", "deletions"}`` summed over the date range."""
    insertions, deletions = _numstat_totals(repo_root, since, until)
    return {"insertions": insertions, "deletions": deletions}


def refactor_to_addition_ratio(repo_root: str, since: str, until: str) -> float | None:
    """Return deletions/insertions over the range, or ``None`` when no additions.

    A populated range with zero deletions returns ``0.0``; a range with zero
    insertions returns ``None`` (avoids ZeroDivisionError and signals no data).
    """
    insertions, deletions = _numstat_totals(repo_root, since, until)
    if insertions == 0:
        return None
    return deletions / insertions


# ── module_size_trend / cap_change_events (ticket 21de-f9d9) ────────────────
#
# A "qualified" revision is a reachable commit whose tree has a positive-integer
# ``.github/module-size-limit.txt`` blob AND at least one tracked
# ``src/rebar/**/*.py`` blob. Both metrics walk the same qualified, date-filtered,
# (committer_timestamp, sha)-sorted history; they differ only in what they report
# once that history is in hand.


@dataclass(frozen=True)
class _Revision:
    """One qualified revision: its identity, historical cap, and py-module blobs."""

    sha: str
    timestamp: str
    cap: int
    py_blobs: dict[str, str]  # repo-relative path -> blob sha, at this revision


def _normalize_git_iso(timestamp: str) -> str:
    """Return a Python ISO timestamp, spelling UTC as ``+00:00``."""
    return datetime.fromisoformat(timestamp.strip().replace("Z", "+00:00")).isoformat()


def _list_commits(repo_root: str) -> list[tuple[str, str]]:
    """Return ``(sha, committer_iso)`` for every commit reachable from ``HEAD``."""
    out = _git(repo_root, "log", "--format=%H%x09%cI")
    commits = []
    for line in out.splitlines():
        if not line.strip():
            continue
        sha, _, timestamp = line.partition("\t")
        commits.append((sha, _normalize_git_iso(timestamp)))
    return commits


def _read_cap_at(repo_root: str, sha: str) -> int | Literal["invalid"] | None:
    """Return positive-int cap at ``sha``, ``None`` if missing, or ``"invalid"``.

    A missing blob is the ordinary "not qualified" case (``git cat-file`` exits
    non-zero), not a failure — checked with ``check=False`` rather than via ``_git``.
    """
    proc = subprocess.run(
        ["git", "cat-file", "-p", f"{sha}:{_CAP_FILE}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT,
        check=False,
    )
    if proc.returncode != 0:
        return None
    try:
        cap = int(proc.stdout.strip())
    except ValueError:
        return "invalid"
    return cap if cap > 0 else "invalid"


def _py_blobs_at(repo_root: str, sha: str) -> dict[str, str]:
    """Return ``{path: blob_sha}`` for tracked ``src/rebar/**/*.py`` blobs at ``sha``."""
    out = _git(repo_root, "ls-tree", "-r", sha, "--", "src/rebar")
    blobs: dict[str, str] = {}
    for line in out.splitlines():
        meta, sep, path = line.partition("\t")
        if not sep or not path.endswith(".py"):
            continue
        parts = meta.split()
        if len(parts) == 3 and parts[1] == "blob":
            blobs[path] = parts[2]
    return blobs


def _git_failure_reason(exc: Exception) -> str:
    """Categorize a git-history read failure into a human-readable reason."""
    if isinstance(exc, subprocess.TimeoutExpired):
        return "git command timed out while reading repository history"
    if isinstance(exc, subprocess.CalledProcessError):
        detail = (exc.stderr or "").strip() or str(exc)
        return f"git command failed while reading repository history: {detail}"
    return f"git history unavailable: {exc}"


def _qualified_history(
    repo_root: str, since: str | None, until: str | None
) -> list[_Revision] | Unavailable:
    """Return >=2 qualified, date-filtered, chronologically sorted revisions.

    Returns a categorized :class:`Unavailable` — never a placeholder — for a
    non-Git/failing repository, a date range with no commits, no positive-integer
    cap blob anywhere in range, no qualifying Python modules anywhere in range, or
    fewer than two qualified revisions once both requirements are met together.
    """
    since_d = _parse_date(since)
    until_d = _parse_date(until)
    try:
        git_root = Path(_git(repo_root, "rev-parse", "--show-toplevel").strip()).resolve()
        if git_root != Path(repo_root).resolve():
            return Unavailable(reason="not a Git repository root", accruing_since=_ACCRUING_SINCE)
        in_range = [
            (sha, ts) for sha, ts in _list_commits(repo_root) if _in_range(ts, since_d, until_d)
        ]
        cap_seen = False
        invalid_cap_seen = False
        qualified: list[_Revision] = []
        for sha, ts in in_range:
            cap = _read_cap_at(repo_root, sha)
            if isinstance(cap, str):
                invalid_cap_seen = True
                continue
            if cap is None:
                continue
            cap_seen = True
            py_blobs = _py_blobs_at(repo_root, sha)
            if py_blobs:
                qualified.append(_Revision(sha=sha, timestamp=ts, cap=cap, py_blobs=py_blobs))
    except (subprocess.SubprocessError, OSError, RuntimeError) as exc:
        return Unavailable(reason=_git_failure_reason(exc), accruing_since=_ACCRUING_SINCE)

    if not in_range:
        return Unavailable(
            reason="no commits in the requested date range", accruing_since=_ACCRUING_SINCE
        )
    if not cap_seen:
        reason = (
            f"no positive-integer module-size cap ({_CAP_FILE}) found in the requested date range"
        )
        if invalid_cap_seen:
            reason = f"invalid module-size cap ({_CAP_FILE}) in the requested date range"
        return Unavailable(reason=reason, accruing_since=_ACCRUING_SINCE)
    if not qualified:
        return Unavailable(
            reason="no tracked src/rebar Python modules found in the requested date range",
            accruing_since=_ACCRUING_SINCE,
        )

    qualified.sort(key=lambda rev: (datetime.fromisoformat(rev.timestamp), rev.sha))
    if len(qualified) < 2:
        return Unavailable(
            reason=(
                f"only {len(qualified)} qualified revision(s) in the requested date range; "
                "at least two qualified revisions are required; need at least 2"
            ),
            accruing_since=_ACCRUING_SINCE,
        )
    return qualified


def _sample_indices(count: int) -> list[int]:
    """Return at most ``_SAMPLE_SIZE`` evenly spaced indices into ``range(count)``.

    Always includes the first (0) and last (``count - 1``) index. Retains every
    index when ``count <= _SAMPLE_SIZE``.
    """
    if count <= _SAMPLE_SIZE:
        return list(range(count))
    step = (count - 1) / (_SAMPLE_SIZE - 1)
    return [round(i * step) for i in range(_SAMPLE_SIZE)]


def _parse_batch_output(data: bytes) -> dict[str, int]:
    """Parse ``git cat-file --batch`` stdout into ``{blob_sha: newline_count}``."""
    counts: dict[str, int] = {}
    pos = 0
    length = len(data)
    while pos < length:
        newline = data.index(b"\n", pos)
        header = data[pos:newline].decode("utf-8", errors="replace")
        pos = newline + 1
        parts = header.split()
        if len(parts) == 2 and parts[1] == "missing":
            continue
        if len(parts) != 3:
            continue
        sha, size = parts[0], int(parts[2])
        counts[sha] = data[pos : pos + size].count(b"\n")
        pos += size + 1  # skip the object's trailing newline
    return counts


def _blob_newline_counts(repo_root: str, blob_shas: list[str]) -> dict[str, int]:
    """Return ``wc -l``-equivalent newline counts for ``blob_shas`` in one batch call."""
    if not blob_shas:
        return {}
    proc = subprocess.run(
        ["git", "cat-file", "--batch"],
        cwd=repo_root,
        input=("\n".join(blob_shas) + "\n").encode("utf-8"),
        capture_output=True,
        text=False,
        timeout=_GIT_TIMEOUT,
        check=True,
    )
    return _parse_batch_output(proc.stdout)


def _revision_sample(repo_root: str, revision: _Revision) -> dict[str, Any]:
    """Compose one ``module_size_trend`` sample from a qualified revision."""
    counts = _blob_newline_counts(repo_root, list(revision.py_blobs.values()))
    max_loc = max(counts.values()) if counts else 0
    return {
        "sha": revision.sha,
        "timestamp": revision.timestamp,
        "cap": revision.cap,
        "module_count": len(revision.py_blobs),
        "max_loc": max_loc,
    }


def module_size_trend(repo_root: str, since: str | None, until: str | None) -> Any:
    """Oldest-to-newest, bounded-sample module-size history over the date range."""
    qualified = _qualified_history(repo_root, since, until)
    if isinstance(qualified, Unavailable):
        return qualified

    indices = _sample_indices(len(qualified))
    try:
        samples = [_revision_sample(repo_root, qualified[index]) for index in indices]
    except (subprocess.SubprocessError, OSError, RuntimeError) as exc:
        return Unavailable(reason=_git_failure_reason(exc), accruing_since=_ACCRUING_SINCE)
    return {
        "samples": samples,
        "qualified_revisions": len(qualified),
        "sampled_revisions": len(samples),
    }


def cap_change_events(repo_root: str, since: str | None, until: str | None) -> Any:
    """Ordered module-size cap changes across every adjacent qualified revision."""
    qualified = _qualified_history(repo_root, since, until)
    if isinstance(qualified, Unavailable):
        return qualified

    events = [
        {"from": prev.cap, "to": curr.cap, "sha": curr.sha, "timestamp": curr.timestamp}
        for prev, curr in pairwise(qualified)
        if prev.cap != curr.cap
    ]
    return {"events": events, "qualified_revisions": len(qualified)}


# c085 registry integration — single-arg context adapters.

_ACCRUING_SINCE = "2026-01-01T00:00:00+00:00"


def _git_spec(metric_id: str, fn: Any) -> MetricSpec:
    """Build a MetricSpec whose single-arg ``compute`` adapts to the c085 context."""

    def compute(ctx: Any) -> Any:
        if ctx is None:
            return None
        repo_root = getattr(ctx, "repo_root", None)
        return fn(repo_root, getattr(ctx, "since", None), getattr(ctx, "until", None))

    return MetricSpec(
        id=metric_id,
        lens="code_health",
        source="git",
        confidence="high",
        compute=compute,
        accruing_since=_ACCRUING_SINCE,
    )


def _analysis_cache(ctx: Any) -> dict[tuple[object, ...], AnalyzerResult | Unavailable]:
    """Return the cache owned by one metrics evaluation context."""

    cache = getattr(ctx, "analysis_cache", None)
    if cache is None:
        cache = {}
        ctx.analysis_cache = cache
    return cache


def _cached_analysis(
    ctx: Any,
    producer: str,
    inputs: tuple[object, ...],
    analyze: Any,
) -> AnalyzerResult | Unavailable:
    """Run an analyzer once for its immutable repository/input configuration."""

    root = Path(ctx.repo_root)
    key = (producer, str(root.resolve()), *inputs)
    cache = _analysis_cache(ctx)
    if key not in cache:
        cache[key] = analyze(root)
    return cache[key]


def _scc_analysis(ctx: Any) -> AnalyzerResult | Unavailable:
    """Return the cached SCC result for the context's configured scan roots."""

    scan_roots = tuple(str(scan_root) for scan_root in ctx.scan_roots)
    configured = getattr(ctx, "include_extensions", ()) or ()
    extensions = tuple(str(extension) for extension in configured)

    # Narrowing is passed only when configured, so an unconfigured project makes exactly the
    # polyglot call it always made. The extensions join the cache key: two differently
    # narrowed runs describe different file sets and must not share a cached result.
    def analyze(root: Path) -> AnalyzerResult | Unavailable:
        """Run the scc adapter, narrowing by extension only when one is configured."""

        if extensions:
            return scc_loc.analyze(root, ctx.scan_roots, include_extensions=extensions)
        return scc_loc.analyze(root, ctx.scan_roots)

    return _cached_analysis(ctx, "scc", (scan_roots, extensions), analyze)


def _lizard_analysis(ctx: Any) -> AnalyzerResult | Unavailable:
    """Return the cached Lizard result for the context repository."""

    return _cached_analysis(ctx, "lizard", (), lizard_complexity.analyze)


def _jscpd_analysis(ctx: Any) -> AnalyzerResult | Unavailable:
    """Return the cached JSCPD result for the context repository."""

    return _cached_analysis(ctx, "jscpd", (), jscpd_dup.analyze)


def _structural_spec(metric_id: str, fn: Any, analyze: Any) -> MetricSpec:
    """Adapt one cached analyzer payload into a structural metric."""

    def compute(ctx: Any) -> Any:
        if ctx is None:
            return None
        result = analyze(ctx)
        if isinstance(result, Unavailable):
            return result
        return fn(result, ctx)

    return MetricSpec(
        id=metric_id,
        lens="code_health",
        source="structural",
        confidence="high",
        compute=compute,
        accruing_since=_ACCRUING_SINCE,
    )


def register() -> None:
    """Append this module's specs to the c085 REGISTRY (idempotent on id)."""

    existing = {spec.id for spec in REGISTRY}
    specs = [
        _structural_spec(
            "module_size_distribution",
            lambda result, ctx: module_size_distribution(
                result.loc, ctx.size_cap, ctx.size_near_fraction
            ),
            _scc_analysis,
        ),
        _structural_spec(
            "oversized_module_count",
            lambda result, ctx: oversized_module_count(
                result.loc, ctx.size_cap, ctx.size_near_fraction
            ),
            _scc_analysis,
        ),
        _structural_spec(
            "complexity_summary", lambda result, _ctx: result.complexity, _lizard_analysis
        ),
        _structural_spec(
            "duplication_summary", lambda result, _ctx: result.duplication, _jscpd_analysis
        ),
        _git_spec("churn", churn),
        _git_spec("refactor_to_addition_ratio", refactor_to_addition_ratio),
        _git_spec("module_size_trend", module_size_trend),
        _git_spec("cap_change_events", cap_change_events),
    ]
    for spec in specs:
        if spec.id not in existing:
            REGISTRY.append(spec)
            existing.add(spec.id)


register()
