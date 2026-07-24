"""Git and structural code-health metric derivations.

The module-size metrics consume the configured ``scc`` analyzer's normalized
LOC result. Git-history metrics remain deterministic derivations over
``git log --numstat``.
"""

from __future__ import annotations

import subprocess
from datetime import date, datetime
from pathlib import Path
from typing import Any

from rebar.metrics.analyzers import scc_loc
from rebar.metrics.registry import REGISTRY, MetricSpec, Unavailable


def _git(repo_root: str, *args: str) -> str:
    """Run a git subcommand in ``repo_root`` and return its stdout."""
    return subprocess.run(
        ["git", *args],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
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


def _analyzer_spec(metric_id: str, fn: Any) -> MetricSpec:
    """Adapt configured SCC LOC into a structural module-size metric."""

    def compute(ctx: Any) -> Any:
        if ctx is None:
            return None
        result = scc_loc.analyze(Path(ctx.repo_root), ctx.scan_roots)
        if isinstance(result, Unavailable):
            return None
        return fn(result.loc, ctx.size_cap, ctx.size_near_fraction)

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
        _analyzer_spec("module_size_distribution", module_size_distribution),
        _analyzer_spec("oversized_module_count", oversized_module_count),
        _git_spec("churn", churn),
        _git_spec("refactor_to_addition_ratio", refactor_to_addition_ratio),
    ]
    for spec in specs:
        if spec.id not in existing:
            REGISTRY.append(spec)
            existing.add(spec.id)


register()
