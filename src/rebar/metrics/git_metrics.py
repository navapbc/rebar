"""Git-derivation code-health readers (ticket 7931).

Read-only, deterministic derivations over git history and the working tree,
registered into the c085 :data:`~rebar.metrics.registry.REGISTRY`. These pin
the CURRENT module-size mechanism: a single flat cap single-sourced in
``.github/module-size-limit.txt`` (the old per-file ceilings / allowlist ratchet
is gone — do not reference ``module-size-ceilings.txt`` or ``-allowlist.txt``).

The multi-arg derivations (the oracle's direct targets):

- :func:`module_size_distribution` — module counts / near-cap / over-cap /
  max-loc over ``src/rebar/**/*.py`` against the cap read from the limit file.
- :func:`churn` — insertions/deletions summed from ``git log --numstat`` over a
  date range.
- :func:`refactor_to_addition_ratio` — deletions/insertions over the same
  numstat (``None`` when no insertions accrued in the range).
- :func:`cap_change_events` — commits in range that changed the value in
  ``.github/module-size-limit.txt``.

Each is registered into the c085 REGISTRY via a single-arg *context adapter*
(c085's ``MetricSpec.compute`` is ``Callable[[context], value | None]``): the
adapter pulls ``repo_root`` / ``since`` / ``until`` off the context object and
calls the multi-arg derivation.
"""

from __future__ import annotations

import subprocess
from datetime import date, datetime
from pathlib import Path
from typing import Any

from rebar.metrics.registry import REGISTRY, MetricSpec

_LIMIT_FILE = ".github/module-size-limit.txt"
_NEAR_CAP_FRACTION = 0.10


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


def _read_cap(repo_root: str) -> int:
    """Read the single-sourced module-size cap from the limit file."""
    return int((Path(repo_root) / _LIMIT_FILE).read_text(encoding="utf-8").strip())


def module_size_distribution(repo_root: str, ref: str = "HEAD") -> dict[str, int]:
    """Summarize ``src/rebar/**/*.py`` line counts against the flat cap.

    Returns ``count`` (# of .py modules under ``src/rebar``), ``near_cap_count``
    (modules within 10% of the cap but not over it), ``over_cap_count`` (modules
    strictly exceeding the cap), and ``max_loc`` (largest module line count).

    The cap is read from ``<repo_root>/.github/module-size-limit.txt`` (never
    hardcoded). The working tree at ``repo_root`` is scanned; ``ref`` is accepted
    for API symmetry (the oracle uses the current tree / HEAD).
    """
    cap = _read_cap(repo_root)
    near_threshold = cap * (1 - _NEAR_CAP_FRACTION)
    src = Path(repo_root) / "src" / "rebar"

    count = 0
    near_cap_count = 0
    over_cap_count = 0
    max_loc = 0
    for path in sorted(src.rglob("*.py")):
        if not path.is_file():
            continue
        loc = len(path.read_text(encoding="utf-8").splitlines())
        count += 1
        max_loc = max(max_loc, loc)
        if loc > cap:
            over_cap_count += 1
        elif loc >= near_threshold:
            near_cap_count += 1

    return {
        "count": count,
        "near_cap_count": near_cap_count,
        "over_cap_count": over_cap_count,
        "max_loc": max_loc,
    }


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


def cap_change_events(repo_root: str, since: str, until: str) -> list[dict[str, Any]]:
    """Return commits in range that changed the value of the limit file.

    Parses ``git log -p`` over ``.github/module-size-limit.txt`` and, for each
    commit that changed the limit value, yields
    ``{"commit", "old_limit", "new_limit"}``. Always returns a list (``[]`` when
    nothing changed in the range).
    """
    since_d = _parse_date(since)
    until_d = _parse_date(until)
    out = _git(
        repo_root,
        "log",
        "-p",
        "--format=commit %H %cI",
        "--",
        _LIMIT_FILE,
    )

    events: list[dict[str, Any]] = []
    commit: str | None = None
    commit_in_range = False
    old_limit: int | str | None = None
    new_limit: int | str | None = None

    def _coerce(text: str) -> int | str:
        text = text.strip()
        try:
            return int(text)
        except ValueError:
            return text

    def _flush() -> None:
        if not commit_in_range or commit is None:
            return
        if old_limit is None and new_limit is None:
            return
        if old_limit != new_limit:
            events.append(
                {
                    "commit": commit,
                    "old_limit": old_limit,
                    "new_limit": new_limit,
                }
            )

    for line in out.splitlines():
        if line.startswith("commit "):
            _flush()
            sha, _, cdate = line[len("commit ") :].strip().partition(" ")
            commit = sha
            commit_in_range = _in_range(cdate, since_d, until_d) if cdate else True
            old_limit = None
            new_limit = None
        elif line.startswith("+++") or line.startswith("---"):
            continue
        elif line.startswith("+"):
            new_limit = _coerce(line[1:])
        elif line.startswith("-"):
            old_limit = _coerce(line[1:])
    _flush()

    return events


# ---------------------------------------------------------------------------
# c085 registry integration — single-arg context adapters.
# ---------------------------------------------------------------------------

_ACCRUING_SINCE = "2026-01-01T00:00:00+00:00"


def _spec(metric_id: str, fn: Any, *, ranged: bool) -> MetricSpec:
    """Build a MetricSpec whose single-arg ``compute`` adapts to the c085 context."""

    def compute(ctx: Any) -> Any:
        if ctx is None:
            return None
        repo_root = getattr(ctx, "repo_root", None)
        if ranged:
            return fn(
                repo_root,
                getattr(ctx, "since", None),
                getattr(ctx, "until", None),
            )
        return fn(repo_root)

    return MetricSpec(
        id=metric_id,
        lens="code_health",
        source="git",
        confidence="high",
        compute=compute,
        accruing_since=_ACCRUING_SINCE,
    )


def register() -> None:
    """Append this module's specs to the c085 REGISTRY (idempotent on id)."""

    existing = {spec.id for spec in REGISTRY}
    specs = [
        _spec("module_size_distribution", module_size_distribution, ranged=False),
        _spec("churn", churn, ranged=True),
        _spec("refactor_to_addition_ratio", refactor_to_addition_ratio, ranged=True),
        _spec("cap_change_events", cap_change_events, ranged=True),
    ]
    for spec in specs:
        if spec.id not in existing:
            REGISTRY.append(spec)
            existing.add(spec.id)


register()
