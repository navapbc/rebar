"""Python port of ``ticket-next-batch.sh`` (Tier C, ``REBAR_COMPUTE``).

Deterministic next-batch selector: picks the next batch of unblocked tasks under
an epic that can be worked in parallel without file-level conflicts. This is a
faithful in-process port of the bash script's two embedded ``python3`` heredocs —
the BFS-over-reducer descendant scan and the greedy file-overlap selection — with
the subprocess data sources (``rebar show`` / ``rebar list`` / an ``importlib``
reducer load) replaced by direct calls to :mod:`rebar.reducer` and the shared
read plumbing (:func:`rebar._engine_support.reads.list_states`). Output (text and
``--output json``) and the stderr conflict matrix are byte-identical to the bash
implementation so the dual-run parity gate passes (docs/bash-migration.md §5).

The ``analyze-file-impact`` hook is unwired in rebar (``ANALYZE_IMPACT=""``), so
the bash always falls back to ``extract_files()`` output with an empty
``files_likely_read`` — that fallback is the only path reproduced here.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from rebar._engine_support.next_batch_files import PathConfig, extract_files
from rebar._engine_support.output import error_envelope
from rebar._engine_support.resolver import resolve_ticket_id
from rebar.reducer import reduce_all_tickets, reduce_ticket

_CLOSED_STATUSES = {"closed", "done", "completed", "deleted"}
# Files that are shared-by-design and support concurrent additive edits.
_OVERLAP_SAFE_FILES = {".test-index"}
# Awaiting-tag constants (formerly sourced from figma-tags.conf / planning-tags.conf).
_DESIGN_AWAITING_IMPORT_TAG = "design:awaiting_import"
_MANUAL_AWAITING_USER_TAG = "manual:awaiting_user"


# ───────────────────────────── candidate model ───────────────────────────────
class _Candidate:
    __slots__ = ("id", "title", "priority", "itype", "status", "files", "files_read")

    def __init__(self, raw: dict, cfg: PathConfig, body: str) -> None:
        self.id = raw.get("id", "")
        self.title = raw.get("title", "untitled")
        self.priority = raw.get("priority", 4)
        self.itype = raw.get("issue_type", "task")
        self.status = raw.get("status", "open").lower()
        text = (raw.get("description") or "") + " " + (raw.get("notes") or "") + " " + body
        seed_files = extract_files(text, cfg)
        # ANALYZE_IMPACT is unwired in rebar → always the extract_files fallback.
        self.files = seed_files
        self.files_read: set[str] = set()
        declared = {
            e["path"]
            for e in (raw.get("file_impact") or [])
            if isinstance(e, dict) and e.get("path")
        }
        if declared:
            self.files = set(self.files) | declared


class EpicNotFound(Exception):
    """Raised when the epic id cannot be resolved/loaded (exit 1)."""

    def __init__(self, epic_id: str, *, empty: bool = False) -> None:
        self.epic_id = epic_id
        self.empty = empty
        super().__init__(epic_id)


class NextBatchResult:
    """Everything both renderings (text/json) and the conflict matrix need."""

    __slots__ = (
        "epic_id",
        "epic_title",
        "batch",
        "candidates",
        "skipped_overlap",
        "skipped_blocked_story",
        "skipped_design_awaiting",
        "skipped_manual_awaiting",
        "skipped_in_progress",
        "skipped_needs_planning",
    )


# ───────────────────────────── core computation ──────────────────────────────
def compute(tracker: str, epic_id: str, *, limit: int = 0) -> NextBatchResult:
    """Compute the next batch under ``epic_id``. ``limit`` of 0 means unlimited.

    Raises :class:`EpicNotFound` when the epic cannot be resolved."""
    cfg = PathConfig()

    # Resolve the canonical epic id + title (bash: ``rebar show <epic>``).
    resolved = resolve_ticket_id(epic_id, tracker)
    if resolved is None:
        raise EpicNotFound(epic_id)
    try:
        epic_state = reduce_ticket(os.path.join(tracker, resolved))
    except Exception:
        epic_state = None
    if not isinstance(epic_state, dict) or epic_state.get("status") is None:
        raise EpicNotFound(epic_id)
    epic_id = resolved
    epic_title = epic_state.get("title", "")

    # Full reduced set (every status, incl. archived/deleted dirs) for the BFS
    # parent map and ticket-body/parent/tag lookups — bash scans all dirs.
    full_states = reduce_all_tickets(tracker, exclude_archived=False, exclude_deleted=False)
    state_by_id: dict[str, dict] = {}
    parent_map: dict[str, list[str]] = {}
    for s in full_states:
        tid = s.get("ticket_id")
        if not tid:
            continue
        state_by_id[tid] = s
        pid = s.get("parent_id") or None
        if pid:
            parent_map.setdefault(pid, []).append(tid)

    # BFS from the epic to find all descendants.
    descendants: set[str] = set()
    queue = [epic_id]
    while queue:
        pid = queue.pop(0)
        for child in parent_map.get(pid, []):
            if child not in descendants:
                descendants.add(child)
                queue.append(child)
    parent_ids_with_children = {pid for pid in parent_map if pid in descendants and parent_map[pid]}

    # Open/in_progress tickets via the shared list path (== ``rebar list``).
    from rebar._engine_support import reads as _reads

    all_tickets = _reads.list_states(tracker, status="open,in_progress")

    ticket_status_map = {
        t.get("ticket_id", ""): t.get("status", "").lower()
        for t in all_tickets
        if t.get("ticket_id")
    }
    # Tombstone override: .tombstone.json carries the terminal status (the reducer
    # does not read it, so list returns the pre-delete status).
    if tracker and os.path.isdir(tracker):
        for entry in os.scandir(tracker):
            if not entry.is_dir():
                continue
            tb = os.path.join(entry.path, ".tombstone.json")
            if os.path.isfile(tb):
                try:
                    with open(tb) as tbf:
                        ts = json.loads(tbf.read())
                    ticket_status_map[entry.name] = str(ts.get("status", "deleted")).lower()
                except Exception:
                    ticket_status_map[entry.name] = "deleted"

    # Derive ready (unblocked) tasks scoped to the epic's descendants.
    ready_tasks = []
    for t in all_tickets:
        tid = t.get("ticket_id", "")
        status = t.get("status", "").lower()
        if status not in ("open", "in_progress"):
            continue
        open_depends_on = [
            d
            for d in (t.get("deps") or [])
            if d.get("relation") == "depends_on"
            and ticket_status_map.get(d.get("target_id", ""), "closed") not in _CLOSED_STATUSES
        ]
        if open_depends_on:
            continue
        if descendants and tid not in descendants:
            continue
        ready_tasks.append(
            {
                "id": tid,
                "priority": t.get("priority", 4),
                "status": status,
                "title": t.get("title", "untitled"),
                "issue_type": t.get("ticket_type", "task"),
                "dependencies": t.get("deps", []),
                "description": t.get("description", ""),
                "file_impact": t.get("file_impact", []),
            }
        )

    # ── Parent-story gate helpers (lookups over the full reduced set) ──────────
    def find_parent_story(task_id: str) -> str | None:
        s = state_by_id.get(task_id) or {}
        return s.get("parent_id") or None

    def is_parent_story_blocked(task_id: str) -> str | None:
        pid = find_parent_story(task_id)
        # A blocked story has an open depends_on (it is in all_tickets but not ready).
        if pid and pid in _blocked_ids:
            return pid
        return None

    def is_parent_story_awaiting(task_id: str, tag: str) -> str | None:
        pid = find_parent_story(task_id)
        if not pid:
            return None
        tags = (state_by_id.get(pid) or {}).get("tags") or []
        if tag in tags:
            return pid
        return None

    # Blocked-id set: active tickets carrying an open depends_on.
    _blocked_ids: set[str] = set()
    for t in all_tickets:
        tid = t.get("ticket_id", "")
        status = t.get("status", "").lower()
        if status not in ("open", "in_progress"):
            continue
        if any(
            d.get("relation") == "depends_on"
            and ticket_status_map.get(d.get("target_id", ""), "closed") not in _CLOSED_STATUSES
            for d in (t.get("deps") or [])
        ):
            _blocked_ids.add(tid)

    # ── Build candidate list (skip stories, blocked/awaiting parents) ─────────
    skipped_blocked_story = []
    skipped_design_awaiting = []
    skipped_manual_awaiting = []
    skipped_in_progress = []
    skipped_needs_planning = []
    candidates_raw = []

    for raw in ready_tasks:
        tid = raw.get("id", "")
        title = raw.get("title", "untitled")
        status = raw.get("status", "open").lower()

        if status == "in_progress":
            skipped_in_progress.append((tid, title))
            continue
        if tid in parent_ids_with_children:
            continue
        ttype = raw.get("issue_type", raw.get("ticket_type", "task")).lower()
        if ttype == "story" and tid not in parent_ids_with_children:
            skipped_needs_planning.append((tid, title))
            continue
        blocked_parent = is_parent_story_blocked(tid)
        if blocked_parent:
            skipped_blocked_story.append((tid, title, blocked_parent))
            continue
        design_awaiting_parent = is_parent_story_awaiting(tid, _DESIGN_AWAITING_IMPORT_TAG)
        if design_awaiting_parent:
            skipped_design_awaiting.append((tid, title, design_awaiting_parent))
            continue
        if cfg.planning_flag_enabled:
            manual_awaiting_parent = is_parent_story_awaiting(tid, _MANUAL_AWAITING_USER_TAG)
            if manual_awaiting_parent:
                skipped_manual_awaiting.append((tid, title, manual_awaiting_parent))
                continue
        candidates_raw.append(raw)

    def _body(tid: str) -> str:
        s = state_by_id.get(tid)
        if not s:
            return ""
        parts = []
        if s.get("title"):
            parts.append(s["title"])
        for comment in s.get("comments") or []:
            b = comment.get("body", "")
            if b:
                parts.append(b)
        return "\n".join(parts)

    candidates = [_Candidate(raw, cfg, _body(raw.get("id", ""))) for raw in candidates_raw]
    # Sort by priority (0=critical), then id for stable tie-breaking.
    candidates.sort(key=lambda c: (c.priority, c.id))

    # ── Greedy selection with file-overlap ────────────────────────────────────
    claimed_files: dict[str, str] = {}
    batch: list[_Candidate] = []
    skipped_overlap = []
    for c in candidates:
        if limit > 0 and len(batch) >= limit:
            break
        conflict_file = None
        conflict_task = None
        # Iterate in sorted order so the *reported* conflict_file is deterministic.
        # The bash original iterates an unordered set, so when a candidate overlaps
        # on >1 file its conflict_file/conflict_with diagnostic coin-flips per run
        # under hash randomization — a latent nondeterminism in a selector whose
        # contract is "deterministic". This does NOT change batch composition (a
        # ticket is skipped iff ANY non-safe file is already claimed, which is
        # order-independent); it only stabilizes which overlapping file is named.
        for f in sorted(c.files):
            if f in _OVERLAP_SAFE_FILES:
                continue
            if f in claimed_files:
                conflict_file = f
                conflict_task = claimed_files[f]
                break
        if conflict_file:
            skipped_overlap.append((c.id, c.title, conflict_file, conflict_task))
            continue
        batch.append(c)
        for f in c.files:
            claimed_files[f] = c.id

    result = NextBatchResult()
    result.epic_id = epic_id
    result.epic_title = epic_title
    result.batch = batch
    result.candidates = candidates
    result.skipped_overlap = skipped_overlap
    result.skipped_blocked_story = skipped_blocked_story
    result.skipped_design_awaiting = skipped_design_awaiting
    result.skipped_manual_awaiting = skipped_manual_awaiting
    result.skipped_in_progress = skipped_in_progress
    result.skipped_needs_planning = skipped_needs_planning
    return result


# ───────────────────────────── rendering ─────────────────────────────────────
def to_json_dict(r: NextBatchResult) -> dict[str, Any]:
    return {
        "epic_id": r.epic_id,
        "epic_title": r.epic_title,
        "batch_size": len(r.batch),
        "available_pool": len(r.candidates),
        "batch": [
            {
                "id": c.id,
                "title": c.title,
                "priority": c.priority,
                "type": c.itype,
                "files": sorted(c.files),
                "files_likely_read": sorted(c.files_read),
            }
            for c in r.batch
        ],
        "skipped_overlap": [
            {"id": tid, "title": title, "conflict_file": cf, "conflict_with": ct}
            for tid, title, cf, ct in r.skipped_overlap
        ],
        "skipped_blocked_story": [
            {"id": tid, "title": title, "blocked_story": sid}
            for tid, title, sid in r.skipped_blocked_story
        ],
        "skipped_design_awaiting": [
            {"id": tid, "title": title, "blocked_story": sid}
            for tid, title, sid in r.skipped_design_awaiting
        ],
        "skipped_manual_awaiting": [
            {"id": tid, "title": title, "blocked_story": sid}
            for tid, title, sid in r.skipped_manual_awaiting
        ],
        "skipped_in_progress": [
            {"id": tid, "title": title} for tid, title in r.skipped_in_progress
        ],
        "skipped_needs_planning": [
            {"id": tid, "title": title} for tid, title in r.skipped_needs_planning
        ],
    }


def render_text(r: NextBatchResult) -> str:
    lines = [
        f"EPIC: {r.epic_id}\t{r.epic_title}",
        f"AVAILABLE_POOL: {len(r.candidates)}",
        f"BATCH_SIZE: {len(r.batch)}",
    ]
    for c in r.batch:
        lines.append(f"TASK: {c.id}\tP{c.priority}\t{c.itype}\t{c.title}")
    for tid, _title, cf, ct in r.skipped_overlap:
        lines.append(f"SKIPPED_OVERLAP: {tid}\tdeferred (overlaps with {ct} on {cf})")
    for tid, _title, sid in r.skipped_blocked_story:
        lines.append(f"SKIPPED_BLOCKED_STORY: {tid}\tdeferred (parent story {sid} is blocked)")
    for tid, _title, sid in r.skipped_design_awaiting:
        lines.append(
            f"SKIPPED_DESIGN_AWAITING: {tid}\tdeferred "
            f"(parent story {sid} awaiting designer import)"
        )
    for tid, _title, sid in r.skipped_manual_awaiting:
        lines.append(
            f"SKIPPED_MANUAL_AWAITING: {tid}\tdeferred "
            f"(parent story {sid} awaiting manual user step)"
        )
    for tid, _title in r.skipped_in_progress:
        lines.append(f"SKIPPED_IN_PROGRESS: {tid}\talready in_progress")
    for tid, _title in r.skipped_needs_planning:
        lines.append(
            f"SKIPPED_NEEDS_PLANNING: {tid}\tneeds implementation planning (story has 0 children)"
        )
    return "\n".join(lines)


def render_conflict_matrix(candidates: list[_Candidate]) -> str:
    """Human-readable NxN conflict matrix (bash prints this to stderr). Returns
    an empty string when fewer than 2 candidates (bash prints nothing)."""
    if len(candidates) < 2:
        return ""
    ids = [c.id for c in candidates]
    file_sets = {c.id: c.files for c in candidates}
    overlaps: dict[tuple[str, str], set[str]] = {}
    for i, a in enumerate(ids):
        for j, b in enumerate(ids):
            if i < j:
                shared = file_sets[a] & file_sets[b]
                if shared:
                    overlaps[(min(a, b), max(a, b))] = shared
    col_w = max(len(tid) for tid in ids) + 1
    header = " " * col_w + "".join(tid.ljust(col_w) for tid in ids)
    out = ["", "Conflict Matrix:", header]
    for a in ids:
        row = a.ljust(col_w)
        for b in ids:
            if a == b:
                cell = "."
            else:
                key = (min(a, b), max(a, b))
                cell = "X" if key in overlaps else "."
            row += cell.ljust(col_w)
        out.append(row)
    if overlaps:
        out.append("")
        for (a, b), shared in sorted(overlaps.items()):
            out.append(f"  {a} <-> {b}: {', '.join(sorted(shared))}")
    out.append("")
    return "\n".join(out)


# ───────────────────────────── library entrypoint ────────────────────────────
def next_batch_state(tracker: str, epic_id: str, *, limit: int = 0) -> dict[str, Any]:
    """In-process next-batch for the library/MCP. Raises
    :class:`rebar._engine_support.reads.ReadError` on a missing epic (mapped to
    the library's exit-1 contract by the caller)."""
    from rebar._engine_support.reads import ReadError

    try:
        result = compute(tracker, epic_id, limit=limit)
    except EpicNotFound:
        raise ReadError(f"Could not load epic '{epic_id}'") from None
    return to_json_dict(result)


# ───────────────────────────── CLI entrypoint ────────────────────────────────
_USAGE = "rebar next-batch <epic-id> [--limit=N|unlimited] [--output json]"


def run(argv: list[str], tracker: str) -> int:
    """CLI handler for ``next-batch`` (text/json, exit codes 0/1/2). Mirrors the
    bash dispatcher arm byte-for-byte."""
    epic_id = ""
    limit = 0
    limit_zero = False
    json_output = False

    # --output/-o resolution (report profile).
    rest: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--output", "-o"):
            if i + 1 >= len(argv):
                print(f"Usage: {_USAGE}", file=sys.stderr)
                return 2
            fmt = argv[i + 1]
            i += 2
            if fmt == "json":
                json_output = True
            elif fmt in ("text", "report"):
                json_output = False
            else:
                print(f"Error: unknown --output format '{fmt}'", file=sys.stderr)
                return 2
            continue
        if a.startswith("--output="):
            fmt = a.split("=", 1)[1]
            if fmt == "json":
                json_output = True
            elif fmt in ("text", "report"):
                json_output = False
            else:
                print(f"Error: unknown --output format '{fmt}'", file=sys.stderr)
                return 2
            i += 1
            continue
        rest.append(a)
        i += 1

    for arg in rest:
        if arg.startswith("--limit="):
            val = arg[len("--limit=") :]
            if val == "unlimited":
                limit = 0
            elif not val.isdigit():
                print(
                    "Error: --limit must be a non-negative integer or 'unlimited'",
                    file=sys.stderr,
                )
                return 2
            else:
                limit = int(val)
                if val == "0" or limit == 0:
                    limit_zero = True
        elif arg in ("--help", "-h"):
            print(f"Usage: {_USAGE}")
            return 0
        elif arg.startswith("-"):
            print(f"Unknown flag: {arg}", file=sys.stderr)
            print(f"Usage: {_USAGE}", file=sys.stderr)
            return 2
        else:
            if not epic_id:
                epic_id = arg
            else:
                print(
                    "Error: Multiple epic IDs provided. Expected exactly one.",
                    file=sys.stderr,
                )
                return 2

    if not epic_id:
        print(f"Usage: {_USAGE}", file=sys.stderr)
        return 2

    # --limit=0 early exit: empty batch immediately.
    if limit_zero:
        if json_output:
            print('{"epic_id":"' + epic_id + '","batch_size":0,"tasks":[]}')
        else:
            print("BATCH_SIZE: 0")
        return 0

    try:
        result = compute(tracker, epic_id, limit=limit)
    except EpicNotFound as exc:
        if json_output:
            print(
                json.dumps(
                    error_envelope(
                        "ticket_not_found",
                        exc.epic_id,
                        f"Could not load epic '{exc.epic_id}'",
                        1,
                    ),
                    ensure_ascii=False,
                )
            )
        print(f"Error: Could not load epic {exc.epic_id}", file=sys.stderr)
        return 1

    matrix = render_conflict_matrix(result.candidates)
    if matrix:
        print(matrix, file=sys.stderr)

    if json_output:
        print(json.dumps(to_json_dict(result), indent=2))
    else:
        print(render_text(result))
    return 0
