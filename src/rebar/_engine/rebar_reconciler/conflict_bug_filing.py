"""Deferred conflict bug filing — the hardened reconciler-conflict filer.

Extracted from ``applier.py`` (which sits at the module-size hard cap) for
ticket 4527-0cfa-d31a-4a08. ``applier.apply()`` collects ``pending_bug_ticket``
directives during the inbound dispatch loop and calls
:func:`file_conflict_bug_ticket` for each AFTER ``_apply_batch`` returns —
outside the HEAD-drift guard's scope (bug d822's deferred-filing contract).

Hardening contract (see ``docs/bug-creation-contract.md``):

- **dedup** — every (local_id, jira_key) pair maps to a stable tag
  (:func:`conflict_dedup_tag`); if an open bug already carries that tag the
  repeat filing is absorbed into it instead of creating a duplicate.
- **accumulation** — an absorbed repeat posts a ``RECONCILER_CONFLICT:``
  marker comment at most once per 24h window, so a conflict that persists
  across many reconciler passes cannot flood the ticket.
- **abort-if-empty** — a pending payload with no identifiers at all, or an
  empty title/description, is refused loudly (stderr) instead of producing a
  hollow, un-actionable ticket.
- **provenance** — creates carry ``--detected-by reconciler-conflict`` so the
  detected-by taxonomy can attribute the filing channel.

Everything here is stdlib-only and side-effect free apart from the injected
``runner`` (subprocess by default), mirroring the ``scripts/canary_bridge.py``
testing idiom.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path

# argv -> (returncode, stdout, stderr); tests inject a fake.
Runner = Callable[[list[str]], tuple[int, str, str]]

_MARKER = "RECONCILER_CONFLICT:"
_ACCUMULATION_WINDOW_SECS = 24 * 3600
_DETECTED_BY = "reconciler-conflict"


# raw-git-ok: generic command runner, argv supplied by caller
def _default_runner(argv: list[str]) -> tuple[int, str, str]:
    import subprocess

    try:
        res = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", repr(exc)
    return res.returncode, res.stdout, res.stderr


def _utc_ts(epoch: int) -> str:
    return datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def conflict_dedup_tag(local_id: str, jira_key: str) -> str:
    """Stable per-pair dedup tag. NUL separator keeps the pair unambiguous."""
    digest = hashlib.sha1(f"{local_id}\x00{jira_key}".encode()).hexdigest()[:12]
    return f"conflict-{digest}"


def _find_open_conflict_ticket(cli: str, tag: str, runner: Runner) -> str:
    """Fail-soft: '' on any error means "not found" and the caller creates.

    A broken dedup search must degrade to a possible duplicate ticket, never
    to a silently swallowed conflict.
    """
    rc, stdout, _stderr = runner(
        [cli, "list", "--type=bug", "--status=open", f"--has-tag={tag}", "--output", "json"]
    )
    if rc != 0:
        return ""
    try:
        tickets = json.loads(stdout)
        return str(tickets[0]["ticket_id"]) if tickets else ""
    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
        return ""


def _recent_marker_comment(cli: str, tid: str, runner: Runner, now_epoch: int) -> bool:
    """True if the ticket carries a marker comment younger than 24h.

    Fail-soft toward commenting: a duplicate accumulation comment is cheaper
    than a silent gap in the conflict trail.
    """
    rc, stdout, _stderr = runner([cli, "show", tid, "--output", "json"])
    if rc != 0:
        return False
    try:
        data = json.loads(stdout[stdout.find("{") :])
        comments = data.get("comments") or []
    except (json.JSONDecodeError, ValueError, AttributeError):
        return False
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        if not str(comment.get("body", "")).startswith(_MARKER):
            continue
        ts = comment.get("timestamp")
        if not isinstance(ts, (int, float)):
            continue
        secs = ts / 1e9 if ts > 1e12 else ts  # store timestamps are ns
        if now_epoch - secs < _ACCUMULATION_WINDOW_SECS:
            return True
    return False


def file_conflict_bug_ticket(
    cli_path: Path,
    pending: Mapping[str, str],
    *,
    runner: Runner | None = None,
    now_epoch: int | None = None,
) -> str:
    """File (or absorb into) the audit bug for one unresolved conflict pair.

    ``pending`` is the ``pending_bug_ticket`` directive built by
    ``_apply_inbound_conflict``: title, description, parent_id, local_id,
    jira_key. Returns the bug id (create path: last stdout line of
    ``create``; dedup path: the absorbing ticket's id), or '' on
    refusal/failure. Never raises: the deferred filing loop in
    ``applier.apply()`` must stay best-effort.
    """
    if runner is None:
        runner = _default_runner
    if now_epoch is None:
        now_epoch = int(time.time())

    title = str(pending.get("title", ""))
    description = str(pending.get("description", ""))
    parent_id = str(pending.get("parent_id", ""))
    local_id = str(pending.get("local_id", ""))
    jira_key = str(pending.get("jira_key", ""))

    if not local_id and not jira_key:
        print(
            f"{_MARKER} refusing hollow conflict filing: no local_id and no"
            f" jira_key (title={title!r})",
            file=sys.stderr,
        )
        return ""
    if not title.strip() or not description.strip():
        print(
            f"{_MARKER} refusing hollow conflict filing for pair"
            f" ({local_id!r}, {jira_key!r}): empty title or description",
            file=sys.stderr,
        )
        return ""
    if not cli_path.exists():
        return ""

    cli = str(cli_path)
    tag = conflict_dedup_tag(local_id, jira_key)

    tid = _find_open_conflict_ticket(cli, tag, runner)
    if tid:
        if _recent_marker_comment(cli, tid, runner, now_epoch):
            return tid
        body = (
            f"{_MARKER} pair ({local_id!r}, {jira_key!r}) still unresolved as of"
            f" {_utc_ts(now_epoch)} — reconciler surfaced the same conflict again."
        )
        runner([cli, "comment", tid, body])
        return tid

    cmd: list[str] = [
        cli,
        "create",
        "bug",
        title,
        "-d",
        description,
        "--tags",
        tag,
        "--detected-by",
        _DETECTED_BY,
        "--output",
        "json",
    ]
    if parent_id:
        cmd.extend(["--parent", parent_id])
    rc, stdout, _stderr = runner(cmd)
    if rc != 0:
        return ""
    # Parse create's stable JSON shape instead of scraping text lines — the text
    # confirmation is a human channel and not a parse target (ticket 6bda-9d58-8546-4638).
    try:
        return str(json.loads(stdout)["id"])
    except (json.JSONDecodeError, KeyError, TypeError):
        return ""
