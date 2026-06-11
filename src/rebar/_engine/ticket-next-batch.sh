#!/usr/bin/env bash
set -euo pipefail
# ticket-next-batch.sh — Deterministic next-batch selector.
#
# Selects the next batch of unblocked tasks under a given epic that can be worked
# in parallel without file-level conflicts. Dependency-ordered; conflict-free.
#
# Handles the 3-tier hierarchy (epic -> story -> task):
#   - If a story has open blockers, all its child tasks are deferred regardless
#     of their own dependency state.
#   - Tasks with file-level overlap are serialized: only the higher-priority
#     task enters the batch; the lower-priority one defers to the next cycle.
#
# Usage:
#   ticket-next-batch.sh <epic-id>              # All non-conflicting ready tasks
#   ticket-next-batch.sh <epic-id> --limit=N          # Up to N tasks
#   ticket-next-batch.sh <epic-id> --limit=0          # Empty batch (BATCH_SIZE: 0)
#   ticket-next-batch.sh <epic-id> --limit=unlimited  # No cap (same as omitting --limit)
#   ticket-next-batch.sh <epic-id> --output json  # Machine-readable JSON output (-o json)
#
# Text output lines:
#   EPIC: <id>  <title>
#   AVAILABLE_POOL: <n>  (candidates before overlap filtering)
#   BATCH_SIZE: <n>
#   TASK: <id>  P<priority>  <type>  <title>
#   SKIPPED_OVERLAP: <id>  deferred (overlaps with <other-id> on <file>)
#   SKIPPED_BLOCKED_STORY: <id>  deferred (parent story <story-id> is blocked)
#   SKIPPED_IN_PROGRESS: <id>  already in_progress
#   SKIPPED_NEEDS_PLANNING: <id>  needs implementation planning (story has 0 children)
#
# Exit codes:
#   0 — Batch generated (BATCH_SIZE may be 0 if no ready tasks)
#   1 — Epic not found or tk error
#   2 — Usage error

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./rebar-config.sh
source "$SCRIPT_DIR/rebar-config.sh"
# Canonical structured-output flag (--output/-o); logic in ticket_output.py.
# shellcheck source=/dev/null
source "$SCRIPT_DIR/ticket-output.sh"

REPO_ROOT="$(_rebar_root)"
if [ -z "$REPO_ROOT" ]; then
    echo "ERROR: Not in a git repository" >&2
    exit 2
fi

TICKET_CMD="${TICKET_CMD:-${SCRIPT_DIR}/rebar}"
REDUCER="${SCRIPT_DIR}/ticket-reducer.py"

# Optional file-impact analyzer for richer file-overlap detection. Left empty
# in rebar (next-batch falls back to heuristic file extraction); the hook is
# retained so a project can wire in a static analyzer if desired.
ANALYZE_IMPACT=""

# v3 event-sourced ticket system — the only supported backend.
TRACKER_DIR="${TICKETS_TRACKER_DIR:-$REPO_ROOT/.tickets-tracker}"

# Ensure tracker is initialized (worktree startup race condition fix).
# In fresh worktrees, .tickets-tracker is a symlink created by ticket-init.sh.
if [ ! -d "$TRACKER_DIR" ] && [ -z "${TICKETS_TRACKER_DIR:-}" ] && [ -f "$SCRIPT_DIR/ticket-init.sh" ]; then
    _init_stderr=$(bash "$SCRIPT_DIR/ticket-init.sh" --silent 2>&1 >/dev/null) || {
        if [ -n "$_init_stderr" ]; then
            echo "Warning: ticket-init.sh failed — $_init_stderr" >&2
        fi
    }
fi

PYTHON="python3"
READ_CONFIG="${SCRIPT_DIR}/read-config.sh"
_REBAR_CONFIG="$(_rebar_config_file)"

# Read config-driven path patterns for extract_files()
CFG_SRC_DIR=""
CFG_TEST_DIR=""
CFG_TEST_UNIT_DIR=""
CFG_EXTRA_DIR_ROOTS=""
if [ -x "$READ_CONFIG" ] && [ -n "$_REBAR_CONFIG" ]; then
    CFG_SRC_DIR=$("$READ_CONFIG" paths.src_dir "$_REBAR_CONFIG" 2>/dev/null || true)
    CFG_TEST_DIR=$("$READ_CONFIG" paths.test_dir "$_REBAR_CONFIG" 2>/dev/null || true)
    CFG_TEST_UNIT_DIR=$("$READ_CONFIG" paths.test_unit_dir "$_REBAR_CONFIG" 2>/dev/null || true)
    CFG_EXTRA_DIR_ROOTS=$("$READ_CONFIG" paths.extra_dir_roots "$_REBAR_CONFIG" 2>/dev/null || true)
fi
# Defaults for when config is unavailable
CFG_SRC_DIR="${CFG_SRC_DIR:-src}"
CFG_TEST_DIR="${CFG_TEST_DIR:-tests}"
CFG_TEST_UNIT_DIR="${CFG_TEST_UNIT_DIR:-tests/unit}"
CFG_EXTRA_DIR_ROOTS="${CFG_EXTRA_DIR_ROOTS:-}"

# Read flag for manual:awaiting_user filter (the tag constant itself is inlined
# at the embedded-python _MANUAL_AWAITING_USER_TAG below).
_PLANNING_FLAG_ENABLED=false
if [ -x "$READ_CONFIG" ] && [ -n "$_REBAR_CONFIG" ]; then
    _flag_val=$("$READ_CONFIG" planning.external_dependency_block_enabled "$_REBAR_CONFIG" 2>/dev/null || true)
    if [ "$_flag_val" = "true" ] || [ "$_flag_val" = "1" ] || [ "$_flag_val" = "yes" ]; then
        _PLANNING_FLAG_ENABLED=true
    fi
fi

# --- Argument parsing ---

epic_id=""
limit=0          # 0 = unlimited (no cap)
limit_zero=false # true when user explicitly passes --limit=0 (empty batch)
json_output=false

# Resolve --output/-o (report profile: text|json) and strip it from the args.
_resolve_output_format report "$@" || exit 2
[ "$_OUTPUT_FMT" = "json" ] && json_output=true
_strip_output_flags "$@"
set -- ${_OUTPUT_ARGS[@]+"${_OUTPUT_ARGS[@]}"}

for arg in "$@"; do
    case "$arg" in
        --limit=*)
            limit="${arg#--limit=}"
            if [[ "$limit" == "unlimited" ]]; then
                limit=0  # internal 0 = unlimited (no cap)
            elif ! [[ "$limit" =~ ^[0-9]+$ ]]; then
                echo "Error: --limit must be a non-negative integer or 'unlimited'" >&2
                exit 2
            elif [[ "$limit" == "0" ]]; then
                limit_zero=true
            fi
            ;;
        --help|-h)
            sed -n '2,50p' "$0" | grep '^#' | sed 's/^# \?//'
            exit 0
            ;;
        -*)
            echo "Unknown flag: $arg" >&2
            echo "Usage: ticket-next-batch.sh <epic-id> [--limit=N|unlimited] [--output json]" >&2
            exit 2
            ;;
        *)
            if [ -z "$epic_id" ]; then
                epic_id="$arg"
            else
                echo "Error: Multiple epic IDs provided. Expected exactly one." >&2
                exit 2
            fi
            ;;
    esac
done

if [ -z "$epic_id" ]; then
    echo "Usage: ticket-next-batch.sh <epic-id> [--limit=N|unlimited] [--output json]" >&2
    exit 2
fi

# --limit=0 early exit: return empty batch immediately (no task processing needed)
if [ "$limit_zero" = true ]; then
    if [ "$json_output" = true ]; then
        echo '{"epic_id":"'"$epic_id"'","batch_size":0,"tasks":[]}'
    else
        echo "BATCH_SIZE: 0"
    fi
    exit 0
fi

# --- Data collection using ticket CLI ---

tmpdir=$(mktemp -d)
trap 'rm -rf "$tmpdir"' EXIT

# Epic details via ticket show (v3 JSON output).
# Bug 19a3-03ca follow-up: also check ticket show's exit code, not just file
# emptiness. The in-process ticket_show (ticket-lib-api.sh) writes a
# {"error":"ticket_not_found",...} JSON to stdout on miss — so the file is
# never empty and the !-s check alone passes through to BATCH_SIZE: 0 for
# any nonexistent epic. Previously masked by a perf regression that made the
# resolver time-out before reaching this code path.
if ! "$TICKET_CMD" show "$epic_id" >"$tmpdir/epic.txt" 2>/dev/null; then
    _emit_error_envelope ticket_not_found "$epic_id" "Could not load epic '$epic_id'" 1
    echo "Error: Could not load epic $epic_id" >&2
    exit 1
fi
if [ ! -s "$tmpdir/epic.txt" ]; then
    _emit_error_envelope ticket_not_found "$epic_id" "Could not load epic '$epic_id' (empty output)" 1
    echo "Error: Could not load epic $epic_id (empty output)" >&2
    exit 1
fi

# Resolve the canonical full ID from the ticket show JSON response.
epic_id_canonical=$(python3 -c "
import json, sys

txt = open('$tmpdir/epic.txt').read().strip()
try:
    data = json.loads(txt)
    # v3 ticket show returns ticket_id; also accept id for test compat
    print(data.get('ticket_id', data.get('id', '')))
except (json.JSONDecodeError, Exception):
    print('')
" 2>/dev/null)
if [ -n "$epic_id_canonical" ]; then
    epic_id="$epic_id_canonical"
fi

# Descendants of epic — scan ticket data for parent references.
# Handles 3-tier hierarchy: epic -> story -> task (grandchildren).
# v3: scan .tickets-tracker/ dirs via the reducer (reads parent_id from CREATE events)
# v2: scan .tickets/*.md frontmatter for parent: field (legacy)
touch "$tmpdir/epic_children.txt"
touch "$tmpdir/parent_ids_with_children.txt"
if [ -d "$TRACKER_DIR" ]; then
    SPRINT_TRACKER_DIR="$TRACKER_DIR" \
    SPRINT_EPIC_ID_BFS="$epic_id" \
    SPRINT_REDUCER="$REDUCER" \
    python3 - "$tmpdir/epic_children.txt" "$tmpdir/parent_ids_with_children.txt" <<'DESCEOF_V3'
import os, sys, json, importlib.util, pathlib

outfile = sys.argv[1]
parents_outfile = sys.argv[2]
tracker_dir = os.environ["SPRINT_TRACKER_DIR"]
root_id = os.environ["SPRINT_EPIC_ID_BFS"]
reducer_path = os.environ.get("SPRINT_REDUCER", "")

# Load reducer module
reduce_ticket = None
if reducer_path and os.path.exists(reducer_path):
    try:
        spec = importlib.util.spec_from_file_location("ticket_reducer", reducer_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        reduce_ticket = mod.reduce_ticket
    except Exception:
        pass

# Build parent_map by scanning each ticket dir via the reducer
parent_map = {}  # parent_id -> [child_id, ...]
try:
    for tid in os.listdir(tracker_dir):
        tdir = os.path.join(tracker_dir, tid)
        if not os.path.isdir(tdir) or tid.startswith("."):
            continue
        parent_id = None
        if reduce_ticket:
            try:
                state = reduce_ticket(tdir)
                if state and isinstance(state, dict):
                    parent_id = state.get("parent_id") or None
            except Exception:
                pass
        else:
            # Fallback: scan event files for CREATE event parent_id
            try:
                for fname in sorted(os.listdir(tdir)):
                    if not fname.endswith(".json") or fname == ".cache.json":
                        continue
                    try:
                        with open(os.path.join(tdir, fname), encoding="utf-8") as f:
                            ev = json.load(f)
                        if ev.get("event_type") == "CREATE":
                            parent_id = ev.get("data", {}).get("parent_id") or None
                            break
                    except Exception:
                        pass
            except Exception:
                pass
        if parent_id:
            parent_map.setdefault(parent_id, []).append(tid)
except Exception:
    pass

# BFS from root to find all descendants
descendants = set()
queue = [root_id]
while queue:
    pid = queue.pop(0)
    for child in parent_map.get(pid, []):
        if child not in descendants:
            descendants.add(child)
            queue.append(child)

# Identify descendants that have children (stories with impl tasks)
parents_with_children = {pid for pid in parent_map if pid in descendants and parent_map[pid]}
with open(outfile, "w") as f:
    for d in sorted(descendants):
        f.write(d + "\n")
with open(parents_outfile, "w") as f:
    for p in sorted(parents_with_children):
        f.write(p + "\n")
DESCEOF_V3
fi

# All tickets as JSON (used by Python inline for ready/blocked filtering)
"$TICKET_CMD" list --status=open,in_progress > "$tmpdir/all_tickets.json" 2>/dev/null || echo "[]" > "$tmpdir/all_tickets.json"

# --- Core logic ---

TICKET_CMD="$TICKET_CMD" \
SPRINT_TMPDIR="$tmpdir" \
SPRINT_EPIC_ID="$epic_id" \
SPRINT_LIMIT="$limit" \
SPRINT_JSON="$json_output" \
SPRINT_PYTHON="$PYTHON" \
SPRINT_ANALYZE_IMPACT="$ANALYZE_IMPACT" \
SPRINT_REPO_ROOT="$REPO_ROOT" \
SPRINT_CFG_SRC_DIR="$CFG_SRC_DIR" \
SPRINT_CFG_TEST_DIR="$CFG_TEST_DIR" \
SPRINT_CFG_TEST_UNIT_DIR="$CFG_TEST_UNIT_DIR" \
SPRINT_CFG_EXTRA_DIR_ROOTS="$CFG_EXTRA_DIR_ROOTS" \
SPRINT_KNOWN_EXTENSIONLESS_FILES="rebar" \
SPRINT_TRACKER_DIR="$TRACKER_DIR" \
SPRINT_REDUCER="$REDUCER" \
SPRINT_PLANNING_FLAG_ENABLED="$_PLANNING_FLAG_ENABLED" \
python3 - <<'PYEOF'
import json
import os
import re
import subprocess
import sys

tmpdir          = os.environ["SPRINT_TMPDIR"]
epic_id         = os.environ["SPRINT_EPIC_ID"]
limit           = int(os.environ.get("SPRINT_LIMIT", "0"))
json_mode       = os.environ.get("SPRINT_JSON", "false").lower() == "true"
python          = os.environ.get("SPRINT_PYTHON", "python3")
analyze_impact  = os.environ.get("SPRINT_ANALYZE_IMPACT", "")
repo_root       = os.environ.get("SPRINT_REPO_ROOT", "")
cfg_src_dir     = os.environ.get("SPRINT_CFG_SRC_DIR", "src")
cfg_test_dir    = os.environ.get("SPRINT_CFG_TEST_DIR", "tests")
cfg_test_unit_dir = os.environ.get("SPRINT_CFG_TEST_UNIT_DIR", "tests/unit")
cfg_extra_dir_roots = os.environ.get("SPRINT_CFG_EXTRA_DIR_ROOTS", "")
cfg_known_extensionless = os.environ.get("SPRINT_KNOWN_EXTENSIONLESS_FILES", "")
tracker_dir     = os.environ.get("SPRINT_TRACKER_DIR", "")
reducer_path    = os.environ.get("SPRINT_REDUCER", "")

# Load v3 reducer module once if available
_reduce_ticket = None
if reducer_path and os.path.exists(reducer_path):
    try:
        import importlib.util as _ilu
        _spec = _ilu.spec_from_file_location("ticket_reducer", reducer_path)
        _mod = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        _reduce_ticket = _mod.reduce_ticket
    except Exception:
        pass

# Flag: planning.external_dependency_block_enabled (passed from bash section)
_FLAG_ENABLED = os.environ.get("SPRINT_PLANNING_FLAG_ENABLED", "false").lower() in ("true", "1", "yes")

# ── Helpers ──────────────────────────────────────────────────────────────────

def ticket_show(ticket_id):
    """Run ticket show <id> and return a simple dict with id, title, status.

    The v3 ticket CLI returns JSON directly.
    """
    ticket_cmd = os.environ.get("TICKET_CMD", "rebar")
    result = subprocess.run(
        [ticket_cmd, "show", ticket_id],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return {}
    output = result.stdout.strip()
    if not output:
        return {}
    try:
        data = json.loads(output)
        # Normalize: map ticket_id -> id, parent_id -> parent for compat
        if "ticket_id" in data and "id" not in data:
            data["id"] = data["ticket_id"]
        if "parent_id" in data and "parent" not in data:
            data["parent"] = data["parent_id"]
        return data
    except json.JSONDecodeError:
        return {"id": ticket_id, "status": "open"}

def extract_files(text):
    """
    Extract candidate file paths from a task description.
    Returns a set of normalised path strings.

    Lines beginning with acceptance-criteria markers (e.g. "AC Verify:") are
    shell commands used to validate the work, not files that will be modified.
    They are stripped before extraction to prevent false-positive batch
    conflicts when multiple tickets reference the same validation command
    (e.g. "AC Verify: bash scripts/validate.sh --ci").
    """
    if not text:
        return set()

    # Strip acceptance-criteria content: shell commands, not files to be modified.
    # Phase 1: remove entire ## Acceptance Criteria sections (case-insensitive, through next ## or EOF)
    text = re.sub(
        r'(?m)^##\s+ACCEPTANCE\s+CRITERIA\b.*?(?=^##\s|\Z)',
        '', text, flags=re.IGNORECASE | re.DOTALL,
    )
    # Phase 2: strip individual AC-prefixed lines (AC Verify:, AC Check:, etc.)
    AC_LINE_RE = re.compile(
        r'^\s*(?:AC\s+\w[\w\s]*:|Acceptance\s+criteria\s*:)',
        re.IGNORECASE,
    )
    text = "\n".join(
        line for line in text.splitlines() if not AC_LINE_RE.match(line)
    )

    files = set()

    # Backtick-delimited paths (any extension)
    for m in re.finditer(r"`([^`]+\.\w+)`", text):
        files.add(m.group(1).lstrip("./"))

    # Build directory-rooted path regex from config values + fixed dirs
    dir_roots = {cfg_src_dir, cfg_test_dir, "app", ".rebar", "plugins"}
    if cfg_extra_dir_roots:
        for extra in cfg_extra_dir_roots.split(","):
            extra = extra.strip()
            if extra:
                dir_roots.add(extra)
    # Also add bare test_dir variants (e.g. "test" if test_dir is "tests")
    if cfg_test_dir.endswith("s"):
        dir_roots.add(cfg_test_dir[:-1])
    dir_pattern = "|".join(re.escape(d) for d in sorted(dir_roots))

    # Explicit directory-rooted paths in prose (any common extension)
    for m in re.finditer(
        r"\b((?:" + dir_pattern + r")/[\w/\-\.]+\.(?:py|sh|md|json|yaml|toml))\b",
        text,
    ):
        files.add(m.group(1).lstrip("./"))

    # Python module notation
    for m in re.finditer(r"\b((?:" + re.escape(cfg_src_dir) + r"|app)(?:\.\w+)+)\b", text):
        files.add(m.group(1).replace(".", "/") + ".py")

    # Known extension-less dispatcher files passed in from bash (colon-separated).
    # Regexes above require a file extension; these are matched by substring so they
    # participate in overlap detection. Add new entries via SPRINT_KNOWN_EXTENSIONLESS_FILES.
    for path in (p for p in cfg_known_extensionless.split(":") if p):
        if path in text:
            files.add(path)

    # Implied test files for src_dir files
    src_prefix = cfg_src_dir + "/"
    test_unit_prefix = cfg_test_unit_dir + "/"
    implied = set()
    for f in files:
        if f.startswith(src_prefix) and f.endswith(".py"):
            inner = f[len(src_prefix):]
            parts = inner.rsplit("/", 1)
            if len(parts) == 2:
                test_path = f"{test_unit_prefix}{parts[0]}/test_{parts[1]}"
            else:
                test_path = f"{test_unit_prefix}test_{parts[0]}"
            implied.add(test_path)
    files |= implied

    return files

def _load_ticket_body(ticket_id):
    """Return ticket text content for file path extraction.

    v3 (event-sourced): compile ticket state via the reducer; build text
    from title + comment bodies (the markdown body fields in CREATE events
    are not stored separately — file references appear in comments).

    Falls back to empty string on any error.
    """
    if tracker_dir:
        ticket_dir = os.path.join(tracker_dir, ticket_id)
        if os.path.isdir(ticket_dir):
            parts = []
            if _reduce_ticket:
                try:
                    state = _reduce_ticket(ticket_dir)
                    if state and isinstance(state, dict):
                        # Include title for file references mentioned there
                        if state.get("title"):
                            parts.append(state["title"])
                        # Include all comment bodies — file paths live here
                        for comment in state.get("comments") or []:
                            body = comment.get("body", "")
                            if body:
                                parts.append(body)
                except Exception:
                    pass
            else:
                # Fallback: scan event JSON files directly without the reducer
                try:
                    for fname in sorted(os.listdir(ticket_dir)):
                        if not fname.endswith(".json") or fname == ".cache.json":
                            continue
                        try:
                            with open(os.path.join(ticket_dir, fname), encoding="utf-8") as f:
                                ev = json.load(f)
                            etype = ev.get("event_type", "")
                            data = ev.get("data", {})
                            if etype == "CREATE" and data.get("title"):
                                parts.append(data["title"])
                            elif etype == "COMMENT" and data.get("body"):
                                parts.append(data["body"])
                        except Exception:
                            pass
                except Exception:
                    pass
            return "\n".join(parts)
        return ""

    return ""

def analyze_file_impact(seed_files):
    """
    Run analyze-file-impact.py on seed file paths.
    Returns (files_likely_modified, files_likely_read) or (None, None) on failure.
    Falls back gracefully if the script is missing, times out, or fails.
    """
    if not analyze_impact or not os.path.exists(analyze_impact):
        return None, None
    if not seed_files:
        return None, None

    try:
        cmd = [python, analyze_impact, "--root", os.path.join(repo_root, "app")]
        cmd.extend(seed_files)
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            return None, None
        data = json.loads(result.stdout)
        files_likely_modified = set(data.get("files_likely_modified", []))
        files_likely_read = set(data.get("files_likely_read", []))
        return files_likely_modified, files_likely_read
    except (subprocess.TimeoutExpired, json.JSONDecodeError, Exception):
        return None, None

# ── Load epic details ─────────────────────────────────────────────────────────

epic_txt = ""
try:
    with open(os.path.join(tmpdir, "epic.txt")) as f:
        epic_txt = f.read().strip()
except FileNotFoundError:
    pass

if not epic_txt:
    print("Error: Epic data is empty", file=sys.stderr)
    sys.exit(1)

# Extract epic title
epic_title = ""
if epic_txt.startswith('{'):
    try:
        epic_data = json.loads(epic_txt)
        epic_title = epic_data.get("title", "")
    except json.JSONDecodeError:
        pass
else:
    for line in epic_txt.splitlines():
        if line.startswith("# "):
            epic_title = line[2:].strip()
            break

# ── Load epic children IDs (scope ready tasks to this epic) ──────────────────

epic_children_ids = set()
try:
    with open(os.path.join(tmpdir, "epic_children.txt")) as f:
        for line in f:
            tid = line.strip()
            if tid:
                epic_children_ids.add(tid)
except FileNotFoundError:
    pass

# ── Load all tickets from JSON and derive ready/blocked sets ──────────────────

all_tickets = []
try:
    with open(os.path.join(tmpdir, "all_tickets.json")) as f:
        all_tickets = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    pass

# Build a map of ticket_id -> status for dependency target lookup
ticket_status_map = {
    t.get("ticket_id", ""): t.get("status", "").lower()
    for t in all_tickets
    if t.get("ticket_id")
}

# Tombstone override: .tombstone.json written by ticket delete carries the terminal
# status; the reducer does not read it, so ticket list returns the pre-delete status.
# Scan the tracker directory and override status for any tombstoned ticket.
if tracker_dir and os.path.isdir(tracker_dir):
    import json as _json_ts
    for _entry in os.scandir(tracker_dir):
        if not _entry.is_dir():
            continue
        _tb = os.path.join(_entry.path, ".tombstone.json")
        if os.path.isfile(_tb):
            try:
                with open(_tb) as _tbf:
                    _ts = _json_ts.loads(_tbf.read())
                ticket_status_map[_entry.name] = str(_ts.get("status", "deleted")).lower()
            except Exception:
                ticket_status_map[_entry.name] = "deleted"

CLOSED_STATUSES = {"closed", "done", "completed", "deleted"}

blocked_ids = set()
ready_tasks = []
for t in all_tickets:
    tid = t.get("ticket_id", "")
    status = t.get("status", "").lower()
    if status not in ("open", "in_progress"):
        continue
    # Only blocked if there is at least one 'depends_on' dep whose target is
    # not closed. Deps with other relations (e.g. 'relates_to', 'blocks') and
    # deps whose target is already closed do NOT block this ticket.
    open_depends_on = [
        d for d in (t.get("deps") or [])
        if d.get("relation") == "depends_on"
        and ticket_status_map.get(d.get("target_id", ""), "closed") not in CLOSED_STATUSES
    ]
    if open_depends_on:
        blocked_ids.add(tid)
    else:
        # Only include tasks that are descendants of the epic
        if epic_children_ids and tid not in epic_children_ids:
            continue
        ready_tasks.append({
            "id": tid,
            "priority": t.get("priority", 4),
            "status": status,
            "title": t.get("title", "untitled"),
            "issue_type": t.get("ticket_type", "task"),
            "dependencies": t.get("deps", []),
            "description": t.get("description", ""),
            "file_impact": t.get("file_impact", []),
        })

# ── Identify stories and check which are blocked ──────────────────────────────

STORY_TYPES = {"story"}
CLOSED = {"closed", "done", "completed", "deleted"}

status_cache  = {}   # issue_id -> status string
story_map     = {}   # story_id -> raw issue dict
story_blocked = {}   # story_id -> bool

# Load children of epic from dep tree output for story detection
children_txt = ""
try:
    with open(os.path.join(tmpdir, "epic_children.txt")) as f:
        children_txt = f.read().strip()
except FileNotFoundError:
    pass

# Parse dep tree output — each line is a ticket id (possibly indented for depth)
story_children_cache = {}  # story_id -> set of task IDs

def find_parent_story(task_id):
    """Find the parent of a task via ticket show. Returns parent ID or None."""
    data = ticket_show(task_id)
    parent_id = data.get("parent", "")
    if parent_id:
        return parent_id
    return None

def is_parent_story_blocked(task_id):
    """Check if a task's parent story is in the blocked set."""
    parent_id = find_parent_story(task_id)
    if parent_id and parent_id in blocked_ids:
        return parent_id
    return None

# Tag constant (inlined; formerly sourced from planning-tags.conf)
_DESIGN_AWAITING_IMPORT_TAG = "design:awaiting_import"  # from figma-tags.conf
_MANUAL_AWAITING_USER_TAG = "manual:awaiting_user"      # from planning-tags.conf

def is_parent_story_awaiting(task_id, tag):
    """Check if a task's parent story has the given awaiting tag.

    Returns the parent story ID if the tag is present, None otherwise.
    """
    parent_id = find_parent_story(task_id)
    if not parent_id:
        return None
    parent_data = ticket_show(parent_id)
    tags = parent_data.get("tags") or []
    if tag in tags:
        return parent_id
    return None

def is_parent_story_design_awaiting(task_id):
    """Backward-compatible wrapper for design:awaiting_import check."""
    return is_parent_story_awaiting(task_id, _DESIGN_AWAITING_IMPORT_TAG)

# ── Build candidate list ───────────────────────────────────────────────────────

skipped_blocked_story    = []   # (task_id, title, story_id)
skipped_design_awaiting  = []   # (task_id, title, story_id)
skipped_manual_awaiting  = []   # (task_id, title, story_id)
skipped_in_progress    = []   # (task_id, title)
skipped_needs_planning = []   # (task_id, title) — stories with 0 impl children
candidates_raw         = []   # raw task dicts that are eligible

# Load parent IDs with children (pre-computed by BFS in bash section above)
parent_ids_with_children = set()
try:
    with open(os.path.join(tmpdir, "parent_ids_with_children.txt")) as f:
        for line in f:
            tid = line.strip()
            if tid:
                parent_ids_with_children.add(tid)
except FileNotFoundError:
    pass

for raw in ready_tasks:
    tid    = raw.get("id", "")
    title  = raw.get("title", "untitled")
    status = raw.get("status", "open").lower()

    if status == "in_progress":
        skipped_in_progress.append((tid, title))
        continue

    # Skip stories/features that have implementation task children
    if tid in parent_ids_with_children:
        continue

    # Skip stories with 0 children — they need implementation planning first
    ttype = raw.get("issue_type", raw.get("ticket_type", "task")).lower()
    if ttype == "story" and tid not in parent_ids_with_children:
        skipped_needs_planning.append((tid, title))
        continue

    # Story-level blocking: skip if parent story is blocked
    blocked_parent = is_parent_story_blocked(tid)
    if blocked_parent:
        skipped_blocked_story.append((tid, title, blocked_parent))
        continue

    # Design gate: skip if parent story is awaiting designer import
    design_awaiting_parent = is_parent_story_design_awaiting(tid)
    if design_awaiting_parent:
        skipped_design_awaiting.append((tid, title, design_awaiting_parent))
        continue

    # Manual gate: skip if parent story is awaiting user manual step (flag-gated)
    if _FLAG_ENABLED:
        manual_awaiting_parent = is_parent_story_awaiting(tid, _MANUAL_AWAITING_USER_TAG)
        if manual_awaiting_parent:
            skipped_manual_awaiting.append((tid, title, manual_awaiting_parent))
            continue

    candidates_raw.append(raw)

# ── Build candidate objects ───────────────────────────────────────────────────

class Candidate:
    __slots__ = (
        "id", "title", "priority", "itype", "status", "files",
        "files_read",
    )

    def __init__(self, raw):
        self.id               = raw.get("id", "")
        self.title            = raw.get("title", "untitled")
        self.priority      = raw.get("priority", 4)
        self.itype            = raw.get("issue_type", "task")
        self.status           = raw.get("status", "open").lower()
        # Fetch full ticket content for seed file extraction
        full_ticket           = _load_ticket_body(self.id)
        text                  = (raw.get("description") or "") + " " + (raw.get("notes") or "") + " " + full_ticket
        seed_files            = extract_files(text)
        # Static file-impact analysis (for parallel-batch file-overlap detection)
        files_likely_modified, files_likely_read = analyze_file_impact(list(seed_files))
        if files_likely_modified is not None:
            self.files        = files_likely_modified
            self.files_read   = files_likely_read or set()
        else:
            # Fallback: use extract_files() output
            self.files        = seed_files
            self.files_read   = set()
        # Union recorded file_impact paths into the conflict set (applied once,
        # regardless of which branch set self.files above — self.files is a set
        # in both branches, so set(...) | declared is type-safe).
        declared = {e["path"] for e in (raw.get("file_impact") or []) if isinstance(e, dict) and e.get("path")}
        if declared:
            self.files = set(self.files) | declared

candidates = [Candidate(raw) for raw in candidates_raw]

# Sort by priority (0=critical), then id for stable tie-breaking.
candidates.sort(key=lambda c: (c.priority, c.id))

# ── Greedy selection with file-overlap and opus cap ───────────────────────────

# Files that are shared-by-design and support concurrent additive edits.
# These are excluded from the file-overlap conflict check to avoid false
# positive serialization between unrelated tasks.
OVERLAP_SAFE_FILES = {
    ".test-index",
}

claimed_files   = {}   # file -> task_id that claimed it
batch           = []   # Candidate objects in batch

skipped_overlap  = []  # (id, title, conflict_file, conflict_task_id)

for c in candidates:
    # Hard stop if limit reached
    if limit > 0 and len(batch) >= limit:
        break

    # File conflict check (skip overlap-safe shared files)
    conflict_file = None
    conflict_task = None
    for f in c.files:
        if f in OVERLAP_SAFE_FILES:
            continue
        if f in claimed_files:
            conflict_file = f
            conflict_task = claimed_files[f]
            break

    if conflict_file:
        skipped_overlap.append((c.id, c.title, conflict_file, conflict_task))
        continue

    # Add to batch
    batch.append(c)
    for f in c.files:
        claimed_files[f] = c.id

# ── Conflict matrix (stderr) ──────────────────────────────────────────────────

def print_conflict_matrix(candidates):
    """Print a human-readable NxN conflict matrix to stderr.

    Rows and columns are candidate task IDs. Cells show 'X' for conflict
    (shared files in files_likely_modified) or '.' for no conflict.
    Below the matrix, conflicting file paths are listed per pair.
    Skipped entirely when fewer than 2 candidates.
    """
    if len(candidates) < 2:
        return

    ids = [c.id for c in candidates]
    file_sets = {c.id: c.files for c in candidates}

    # Compute pairwise overlaps — keys use lexicographic (min, max) order
    # so that lookup in the matrix row loop matches storage order.
    overlaps = {}  # (id_a, id_b) -> set of shared files
    for i, a in enumerate(ids):
        for j, b in enumerate(ids):
            if i < j:
                shared = file_sets[a] & file_sets[b]
                if shared:
                    overlaps[(min(a, b), max(a, b))] = shared

    # Determine column width (max id length + padding)
    col_w = max(len(tid) for tid in ids) + 1

    # Header row
    header = " " * col_w + "".join(tid.ljust(col_w) for tid in ids)
    print(file=sys.stderr)
    print("Conflict Matrix:", file=sys.stderr)
    print(header, file=sys.stderr)

    # Matrix rows
    for a in ids:
        row = a.ljust(col_w)
        for b in ids:
            if a == b:
                cell = "."
            else:
                key = (min(a, b), max(a, b))
                cell = "X" if key in overlaps else "."
            row += cell.ljust(col_w)
        print(row, file=sys.stderr)

    # Detail: list conflicting files per pair
    if overlaps:
        print(file=sys.stderr)
        for (a, b), shared in sorted(overlaps.items()):
            print(f"  {a} <-> {b}: {', '.join(sorted(shared))}", file=sys.stderr)
    print(file=sys.stderr)

print_conflict_matrix(candidates)

# ── Output ────────────────────────────────────────────────────────────────────

if json_mode:
    print(json.dumps({
        "epic_id":        epic_id,
        "epic_title":     epic_title,
        "batch_size":     len(batch),
        "available_pool": len(candidates),
        "batch": [
            {
                "id":             c.id,
                "title":          c.title,
                "priority": c.priority,
                "type":           c.itype,
                "files":          sorted(c.files),
                "files_likely_read": sorted(c.files_read),
            }
            for c in batch
        ],
        "skipped_overlap": [
            {"id": tid, "title": title, "conflict_file": cf, "conflict_with": ct}
            for tid, title, cf, ct in skipped_overlap
        ],
        "skipped_blocked_story": [
            {"id": tid, "title": title, "blocked_story": sid}
            for tid, title, sid in skipped_blocked_story
        ],
        "skipped_design_awaiting": [
            {"id": tid, "title": title, "blocked_story": sid}
            for tid, title, sid in skipped_design_awaiting
        ],
        "skipped_manual_awaiting": [
            {"id": tid, "title": title, "blocked_story": sid}
            for tid, title, sid in skipped_manual_awaiting
        ],
        "skipped_in_progress": [
            {"id": tid, "title": title}
            for tid, title in skipped_in_progress
        ],
        "skipped_needs_planning": [
            {"id": tid, "title": title}
            for tid, title in skipped_needs_planning
        ],
    }, indent=2))
else:
    print(f"EPIC: {epic_id}\t{epic_title}")
    print(f"AVAILABLE_POOL: {len(candidates)}")
    print(f"BATCH_SIZE: {len(batch)}")
    for c in batch:
        print(f"TASK: {c.id}\tP{c.priority}\t{c.itype}\t{c.title}")
    for tid, title, cf, ct in skipped_overlap:
        print(f"SKIPPED_OVERLAP: {tid}\tdeferred (overlaps with {ct} on {cf})")
    for tid, title, sid in skipped_blocked_story:
        print(f"SKIPPED_BLOCKED_STORY: {tid}\tdeferred (parent story {sid} is blocked)")
    for tid, title, sid in skipped_design_awaiting:
        print(f"SKIPPED_DESIGN_AWAITING: {tid}\tdeferred (parent story {sid} awaiting designer import)")
    for tid, title, sid in skipped_manual_awaiting:
        print(f"SKIPPED_MANUAL_AWAITING: {tid}\tdeferred (parent story {sid} awaiting manual user step)")
    for tid, title in skipped_in_progress:
        print(f"SKIPPED_IN_PROGRESS: {tid}\talready in_progress")
    for tid, title in skipped_needs_planning:
        print(f"SKIPPED_NEEDS_PLANNING: {tid}\tneeds implementation planning (story has 0 children)")

PYEOF
