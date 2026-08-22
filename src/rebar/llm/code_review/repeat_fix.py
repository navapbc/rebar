"""Repeat-fix escalation predicate for the Gerrit bugfix-size gate (ticket 1dd5).

The size floor in :mod:`rebar.llm.code_review.bugfix_size_gate` asks a bug fix for a
plan-review attestation only once its diff clears 150 non-test lines. That misses the OTHER
shape of "a design change wearing a bug label": a *small* fix to a file the branch has
already bug-fixed twice this week. Backtested over ``origin/main`` (see
``scripts/backtest_bugfix_size.py --repeat-fix --labels-from-caused-by``), that signal recalls
more later-culprit fixes than the floor does, and it needs nothing but git history plus the
ticket type — no path allowlist and no CI provider, so it is portable to any environment.

The predicate is deliberately *fail-open*: any git or store trouble yields "no escalation"
rather than an exception, because a gate that cannot read history must not turn an unrelated
infrastructure fault into an ``LLM-Review -1``.
"""

from __future__ import annotations

import logging
import subprocess
import time
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

REPEAT_FIX_WINDOW_DAYS = 7
"""How far back a prior bug fix still counts. Shared with ``scripts/backtest_bugfix_size.py``."""

REPEAT_FIX_MIN_PRIOR = 2
"""Prior bug-fix commits on ONE path that make the change under review a repeat fix."""

_GIT_TIMEOUT = 300
"""Watchdog on the read-only log walks — mirrors ``rebar.metrics.blame``'s generous budget."""

_FIELD_SEP = "\x1f"
_RECORD_SEP = "\x1e"


# raw-git-ok: read-oriented git helper, variable subcommand
def _git(repo_root: str, *args: str) -> str | None:
    """Run ``git -C <repo_root> <args>`` and return stdout, or ``None`` on any failure."""
    try:
        proc = subprocess.run(
            ["git", "-C", repo_root, *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
        )
    except Exception:  # best-effort: any git/OS error → no escalation
        logger.debug("repeat-fix: git %s failed in %s", args[:1], repo_root, exc_info=True)
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _commits_touching(
    path: str, *, repo_root: str, base_ref: str, since: float, until: float
) -> list[tuple[str, str]]:
    """``(sha, message)`` for every NON-MERGE commit on ``base_ref`` that touched ``path``
    within ``[since, until]``. Empty on any git failure (fail-open)."""
    out = _git(
        repo_root,
        "log",
        "--no-merges",
        f"--since={_iso(since)}",
        f"--until={_iso(until)}",
        f"--format=%H{_FIELD_SEP}%B{_RECORD_SEP}",
        base_ref,
        "--",
        path,
    )
    if not out:
        return []
    records = []
    for chunk in out.split(_RECORD_SEP):
        chunk = chunk.strip("\n")
        if not chunk or _FIELD_SEP not in chunk:
            continue
        sha, message = chunk.split(_FIELD_SEP, 1)
        records.append((sha.strip(), message))
    return records


def _is_bug_fix(message: str, *, repo_root: Any, cache: dict[str, bool]) -> bool:
    """True iff ``message``'s ticket ref resolves to a ``bug``.

    Reaches BOTH the trailer resolver and the state read through
    :mod:`~rebar.llm.code_review.bugfix_size_gate`, so the gate and this predicate agree on
    what a bug-fix commit is (and one stub in a test arms both)."""
    from rebar.llm.code_review import bugfix_size_gate as _gate

    try:
        ticket = _gate.ticket_for_commit_message(message or "", repo_root=repo_root)
    except Exception:  # noqa: BLE001 — an unresolvable trailer is simply not a prior fix
        return False
    if not ticket:
        return False
    if ticket not in cache:
        try:
            state = _gate._load_ticket_state(ticket, repo_root=repo_root)
        except Exception:  # noqa: BLE001 — unreadable ticket → not counted
            state = {}
        cache[ticket] = str((state or {}).get("ticket_type") or "") == "bug"
    return cache[ticket]


def repeat_fix_escalates(
    paths: list[str] | tuple[str, ...],
    *,
    repo_root: Any = None,
    base_ref: str = "HEAD",
    at: float | None = None,
) -> tuple[bool, list[str]]:
    """``(escalates, priors)`` for a change touching ``paths``.

    Escalates iff SOME single path in ``paths`` was touched by at least
    ``REPEAT_FIX_MIN_PRIOR`` non-merge bug-fix commits on ``base_ref`` in the
    ``REPEAT_FIX_WINDOW_DAYS`` before ``at`` (default: now). ``priors`` are that path's
    prior-fix SHAs — the evidence the gate's teaching finding names — and are ``[]`` whenever
    the verdict is False. Counting is PER PATH: two fixes to two different files are two
    ordinary fixes, not a repeat fix. Never raises."""
    if not paths:
        return False, []
    until = time.time() if at is None else float(at)
    since = until - REPEAT_FIX_WINDOW_DAYS * 86400.0
    root = str(repo_root or ".")
    type_cache: dict[str, bool] = {}
    for path in paths:
        priors = [
            sha
            for sha, message in _commits_touching(
                str(path), repo_root=root, base_ref=base_ref, since=since, until=until
            )
            if _is_bug_fix(message, repo_root=repo_root, cache=type_cache)
        ]
        if len(priors) >= REPEAT_FIX_MIN_PRIOR:
            return True, priors
    return False, []
