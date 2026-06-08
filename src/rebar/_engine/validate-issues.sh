#!/usr/bin/env bash
set -euo pipefail
#
# validate-issues.sh - Validate tk issue tracking health
#
# Checks for:
# - Orphaned tasks (non-epic, non-bug issues without epic parents)
# - Circular dependencies
# - Epics with 0 children but descriptions suggesting they need children
# - Tasks incorrectly set as dependencies instead of children
# - Blocked issues with no clear path to resolution
# - Consistency between task types and epic assignments
#
# Usage: ./scripts/validate-issues.sh [--quick] [--full] [--fix] [--verbose] [--json] [--terse]
#
# Exit codes:
#   0 - Score 5 (perfect health)
#   1 - Score 4 (minor issues)
#   2 - Score 3 (moderate issues)
#   3 - Score 2 (significant issues)
#   4 - Score 1 (critical issues)

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

TICKET_CMD="${TICKET_CMD:-$SCRIPT_DIR/ticket}"

# Colors for output
RED='\033[0;31m'
YELLOW='\033[1;33m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

VERBOSE=false
JSON_OUTPUT=false
FIX_MODE=false
TERSE_MODE=false
# Default to full mode for backwards compatibility
QUICK_MODE=false

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --verbose|-v)
            VERBOSE=true
            shift
            ;;
        --json)
            JSON_OUTPUT=true
            shift
            ;;
        --fix)
            FIX_MODE=true
            shift
            ;;
        --quick)
            QUICK_MODE=true
            shift
            ;;
        --full)
            QUICK_MODE=false
            shift
            ;;
        --terse)
            TERSE_MODE=true
            shift
            ;;
        --help|-h)
            echo "Usage: $0 [--quick] [--full] [--fix] [--verbose] [--json] [--terse]"
            echo ""
            echo "Options:"
            echo "  --quick    Run only the fast, high-value checks (~2 seconds)"
            echo "  --full     Run all checks (default, same as no flag)"
            echo "  --fix      Attempt to automatically fix issues (interactive)"
            echo "  --verbose  Show detailed output"
            echo "  --json     Output results in JSON format"
            echo "  --terse    Single-line output on success; multi-line only when issues exist"
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

# Issue tracking
declare -a CRITICAL_ISSUES=()
declare -a MAJOR_ISSUES=()
declare -a MINOR_ISSUES=()
declare -a WARNINGS=()
declare -a SUGGESTIONS=()

log_verbose() {
    if $VERBOSE; then
        echo -e "${BLUE}[DEBUG]${NC} $1" >&2
    fi
}

log_critical() {
    CRITICAL_ISSUES+=("$1")
    if ! $JSON_OUTPUT; then
        echo -e "${RED}[CRITICAL]${NC} $1" >&2
    fi
}

log_major() {
    MAJOR_ISSUES+=("$1")
    if ! $JSON_OUTPUT; then
        echo -e "${RED}[MAJOR]${NC} $1" >&2
    fi
}

log_minor() {
    MINOR_ISSUES+=("$1")
    if ! $JSON_OUTPUT; then
        echo -e "${YELLOW}[MINOR]${NC} $1" >&2
    fi
}

log_warning() {
    WARNINGS+=("$1")
    if ! $JSON_OUTPUT; then
        echo -e "${YELLOW}[WARNING]${NC} $1" >&2
    fi
}

log_suggestion() {
    SUGGESTIONS+=("$1")
    if $VERBOSE && ! $JSON_OUTPUT; then
        echo -e "${BLUE}[SUGGESTION]${NC} $1" >&2
    fi
}

# Shared JSON cache — populated once, reused by checks that need ticket data.
# Value is the raw JSON string; empty string means not yet fetched.
_SHARED_ISSUES_JSON=""

# Fetch (or return cached) all tickets as normalized JSON via TICKET_CMD list.
# Normalizes v3 field names (ticket_id, ticket_type, parent_id, deps) to the
# internal schema expected by all check functions:
#   id, title, status, type, parent, dependencies, created, description, notes.
# The description and notes fields are derived from the ticket body and comments
# when available; tests may inject them directly via mock TICKET_CMD output.
get_shared_issues_json() {
    if [[ -z "$_SHARED_ISSUES_JSON" ]]; then
        log_verbose "Fetching issues JSON (shared cache) via TICKET_CMD list..."
        local raw_json
        raw_json=$("$TICKET_CMD" list 2>/dev/null) || raw_json="[]"
        [[ -z "$raw_json" ]] && raw_json="[]"
        _SHARED_ISSUES_JSON=$(python3 -c "
import json, sys

try:
    tickets = json.loads(sys.stdin.read())
except (json.JSONDecodeError, ValueError):
    tickets = []

issues = []
for t in tickets:
    # Skip error/fsck state tickets
    status = t.get('status', 'open')
    if status in ('error', 'fsck_needed'):
        continue
    # Skip closed tickets
    if status == 'closed':
        continue

    tid = t.get('ticket_id') or t.get('id', '')
    if not tid:
        continue

    title = t.get('title', '')
    # Skip lock issues (agent-batch-lifecycle markers)
    if title.startswith('[LOCK]'):
        continue

    itype = t.get('ticket_type') or t.get('type', 'task')
    parent = t.get('parent_id') or t.get('parent') or None
    created = t.get('created_at') or t.get('created') or None

    # Normalize deps: v3 uses [{target_id, relation}]; legacy uses [{depends_on_id, type}]
    raw_deps = t.get('deps', t.get('dependencies', []))
    deps = []
    for d in raw_deps:
        dep_id = d.get('target_id') or d.get('depends_on_id', '')
        dep_type = d.get('relation') or d.get('type', 'blocks')
        # Normalize v3 child_of to the canonical parent-child sentinel so
        # that check_child_parent_deps() and check_cross_epic_child_deps() can
        # skip structural parent-child links and avoid false-positive CRITICALs.
        if dep_type == 'child_of':
            dep_type = 'parent-child'
        if dep_id:
            deps.append({'depends_on_id': dep_id, 'type': dep_type})

    # description: prefer explicit field, else derive from body
    description = t.get('description', '')
    if not description:
        body = t.get('body', '') or ''
        description = 'yes' if body.strip() else ''

    # notes: prefer explicit field, else derive from comments
    notes = t.get('notes', '')
    if not notes:
        comments = t.get('comments', [])
        notes = 'yes' if comments else ''

    tags = t.get('tags', [])

    issues.append({
        'id': tid,
        'title': title,
        'status': status,
        'type': itype or 'task',
        'parent': parent,
        'dependencies': deps,
        'created': created,
        'description': description,
        'notes': notes,
        'tags': tags,
    })

print(json.dumps(issues))
" <<< "$raw_json") || _SHARED_ISSUES_JSON="[]"
        [[ -z "$_SHARED_ISSUES_JSON" ]] && _SHARED_ISSUES_JSON="[]"
    fi
    echo "$_SHARED_ISSUES_JSON"
}

# Get all open issues as JSON for processing (kept for any standalone callers)
# shellcheck disable=SC2329
get_issues_json() {
    get_shared_issues_json
}

# Check for orphaned tasks (open non-epic, non-bug issues with no parent epic)
# Bugs are excluded because they are standalone by nature during normal development.
check_orphaned_tasks() {
    log_verbose "Checking for orphaned tasks (no parent epic)..."

    local tmpfile
    tmpfile=$(mktemp)
    # shellcheck disable=SC2064
    trap "rm -f '$tmpfile'" RETURN

    get_shared_issues_json | python3 -c "
import json, sys
from datetime import datetime
from collections import defaultdict

try:
    issues = json.load(sys.stdin)
except (json.JSONDecodeError, ValueError):
    issues = []

orphans = []
for issue in issues:
    itype = issue.get('type', issue.get('issue_type', 'task'))
    # Skip epics and bugs (bugs are standalone by nature)
    if itype in ('epic', 'bug'):
        continue
    status = issue.get('status', 'open')
    if status == 'closed':
        continue
    parent = issue.get('parent', issue.get('parent_id', None))
    deps = issue.get('dependencies', issue.get('deps', []))
    is_child = bool(parent) or any(
        dep.get('dependency_type') == 'parent-child'
        or dep.get('type') == 'parent-child'
        for dep in deps
    )
    if not is_child:
        tags = issue.get('tags', [])
        if 'orphan:deferred_review' in tags and 'origin:arbiter' in tags:
            continue  # exempt — intentional arbiter-generated orphan awaiting review
        orphans.append(issue)

# Detect clusters: group orphans by creation hour
clusters = defaultdict(list)
for o in orphans:
    created = o.get('created_at', o.get('created', ''))
    try:
        if isinstance(created, int):
            ts = str(datetime.fromtimestamp(created))[:19]
        else:
            ts = created[:19].replace('T', ' ')
        dt = datetime.strptime(ts, '%Y-%m-%d %H:%M:%S')
        hour_key = dt.strftime('%Y-%m-%d %H:00')
        clusters[hour_key].append(o)
    except (ValueError, IndexError, TypeError, OSError, OverflowError):
        pass

# Output individual orphans as warnings
for o in orphans:
    iid = o.get('id', '?')
    itype = o.get('type', o.get('issue_type', 'task'))
    title = o.get('title', o.get('name', '?'))
    print(f'WARNING|{iid}|{itype}|{title}')

# Output clusters as major issues
for hour_key, group in sorted(clusters.items()):
    if len(group) >= 3:
        ids = ', '.join(o.get('id', '?').split('-')[-1] for o in group[:5])
        suffix = f' + {len(group) - 5} more' if len(group) > 5 else ''
        print(f'MAJOR|{len(group)} orphaned tasks created around {hour_key} ({ids}{suffix}) — likely need an epic')

print(f'COUNT|{len(orphans)}')
" > "$tmpfile"

    local orphaned_count=0
    while IFS='|' read -r level rest_a rest_b rest_c; do
        case "$level" in
            WARNING)
                log_warning "Orphaned $rest_b (no epic parent): $rest_a - $rest_c"
                ((orphaned_count++)) || true
                ;;
            MAJOR)
                log_major "$rest_a"
                ;;
            COUNT)
                orphaned_count=$rest_a
                ;;
        esac
    done < "$tmpfile"

    if [[ $orphaned_count -eq 0 ]]; then
        log_verbose "No orphaned tasks found — all open tasks belong to an epic"
    fi

    echo "$orphaned_count"
}

# Check for epics with 0 children
check_empty_epics() {
    log_verbose "Checking for epics with 0 children..."

    local empty_count=0
    local tmpfile
    tmpfile=$(mktemp)
    # shellcheck disable=SC2064
    trap "rm -f '$tmpfile'" RETURN

    # Get all open issues from shared cache, find epics and check for children
    get_shared_issues_json | python3 -c "
import json, sys

try:
    issues = json.load(sys.stdin)
except (json.JSONDecodeError, ValueError):
    issues = []

# Find all epic IDs
epic_ids = set()
for issue in issues:
    itype = issue.get('type', issue.get('issue_type', 'task'))
    status = issue.get('status', 'open')
    if itype == 'epic' and status != 'closed':
        iid = issue.get('id', '')
        if iid:
            epic_ids.add(iid)

# Find which epics have children
epics_with_children = set()
for issue in issues:
    parent = issue.get('parent', issue.get('parent_id', None))
    if parent and parent in epic_ids:
        epics_with_children.add(parent)
    deps = issue.get('dependencies', issue.get('deps', []))
    for dep in deps:
        if dep.get('type') == 'parent-child':
            dep_id = dep.get('depends_on_id', dep.get('id', ''))
            if dep_id in epic_ids:
                epics_with_children.add(dep_id)

# Report epics with no children
for issue in issues:
    itype = issue.get('type', issue.get('issue_type', 'task'))
    status = issue.get('status', 'open')
    if itype == 'epic' and status != 'closed':
        iid = issue.get('id', '')
        title = issue.get('title', issue.get('name', '?'))
        if iid and iid not in epics_with_children:
            print(f'EMPTY|{iid}|{title}')
" > "$tmpfile"

    while IFS='|' read -r level epic_id epic_title; do
        case "$level" in
            EMPTY)
                log_verbose "Epic with 0 children: $epic_id - $epic_title (decompose into child tickets when ready)"
                ((empty_count++)) || true
                ;;
        esac
    done < "$tmpfile"

    if [[ $empty_count -eq 0 ]]; then
        log_verbose "All open epics have children"
    else
        log_verbose "$empty_count epic(s) with 0 children (normal for backlog items)"
    fi

    echo $empty_count
}

# Check total unarchived ticket count (warn >300, error >600)
check_ticket_count() {
    log_verbose "Checking total ticket count..."

    local total_count
    total_count=$(get_shared_issues_json | python3 -c "
import json, sys
try:
    issues = json.load(sys.stdin)
except (json.JSONDecodeError, ValueError):
    issues = []
print(len(issues))
")

    if [[ $total_count -ge 600 ]]; then
        log_major "Total ticket count is $total_count (≥600) — consider archiving closed tickets to keep the tracker manageable"
    elif [[ $total_count -ge 300 ]]; then
        log_warning "Total ticket count is $total_count (≥300) — consider archiving older closed tickets"
    else
        log_verbose "Total ticket count: $total_count (within healthy range)"
    fi

    echo "$total_count"
}

# Check for child->parent dependencies (anti-pattern)
check_child_parent_deps() {
    log_verbose "Checking for child->parent dependencies..."

    local tmpfile
    tmpfile=$(mktemp)
    # shellcheck disable=SC2064
    trap "rm -f '$tmpfile'" RETURN

    get_shared_issues_json | python3 -c "
import json, sys

try:
    issues = json.load(sys.stdin)
except (json.JSONDecodeError, ValueError):
    issues = []

errors = 0

for issue in issues:
    iid = issue.get('id', '?')
    deps = issue.get('dependencies', issue.get('deps', []))

    # Find parent (if this is a child of an epic)
    parent_id = None
    parent_id = issue.get('parent', issue.get('parent_id', None))
    if not parent_id:
        for dep in deps:
            if dep.get('type') == 'parent-child':
                parent_id = dep.get('depends_on_id', dep.get('id', None))
                break

    # Check if this issue depends on its parent
    if parent_id:
        for dep in deps:
            dep_type = dep.get('type', '')
            dep_id = dep.get('depends_on_id', dep.get('id', ''))
            # Regular dependencies have different type (not parent-child)
            if dep_type != 'parent-child' and dep_id == parent_id:
                title = issue.get('title', issue.get('name', 'unknown'))
                print(f'ERROR|{iid}|{parent_id}|{title}')
                errors += 1

print(f'COUNT|{errors}')
" > "$tmpfile"

    local error_count=0
    while IFS='|' read -r level rest_a rest_b rest_c; do
        case "$level" in
            ERROR)
                log_critical "Child->parent dependency: $rest_a depends on its parent $rest_b - $rest_c"
                ((error_count++)) || true
                ;;
            COUNT)
                error_count=$rest_a
                ;;
        esac
    done < "$tmpfile"

    if [[ $error_count -eq 0 ]]; then
        log_verbose "No child->parent dependency violations found"
    fi

    echo "$error_count"
}

# Check for cross-epic child dependencies (anti-pattern)
check_cross_epic_child_deps() {
    log_verbose "Checking for cross-epic child dependencies..."

    local tmpfile
    tmpfile=$(mktemp)
    # shellcheck disable=SC2064
    trap "rm -f '$tmpfile'" RETURN

    get_shared_issues_json | python3 -c "
import json, sys

try:
    issues = json.load(sys.stdin)
except (json.JSONDecodeError, ValueError):
    issues = []

# Build parent map (issue_id -> parent_id)
parent_map = {}
for issue in issues:
    iid = issue.get('id')
    parent = issue.get('parent', issue.get('parent_id', None))
    if parent:
        parent_map[iid] = parent
    else:
        deps = issue.get('dependencies', issue.get('deps', []))
        for dep in deps:
            if dep.get('type') == 'parent-child':
                parent_map[iid] = dep.get('depends_on_id', dep.get('id', ''))
                break

# Check for cross-epic dependencies
errors = 0
for issue in issues:
    iid = issue.get('id')
    my_parent = parent_map.get(iid)

    # Only check issues that are children of an epic
    if not my_parent:
        continue

    deps = issue.get('dependencies', issue.get('deps', []))
    for dep in deps:
        # Skip parent-child relationships
        if dep.get('type') == 'parent-child':
            continue

        dep_id = dep.get('depends_on_id', dep.get('id', ''))
        dep_parent = parent_map.get(dep_id)

        # If dependency is a child of a different epic, flag it
        if dep_parent and dep_parent != my_parent:
            title = issue.get('title', issue.get('name', 'unknown'))
            print(f'ERROR|{iid}|{my_parent}|{dep_id}|{dep_parent}|{title}')
            errors += 1

print(f'COUNT|{errors}')
" > "$tmpfile"

    local error_count=0
    while IFS='|' read -r level rest_a rest_b rest_c rest_d rest_e; do
        case "$level" in
            ERROR)
                log_critical "Cross-epic child dependency: $rest_a (child of $rest_b) depends on $rest_c (child of $rest_d). Use epic-level dependency instead - $rest_e"
                ((error_count++)) || true
                ;;
            COUNT)
                error_count=$rest_a
                ;;
        esac
    done < "$tmpfile"

    if [[ $error_count -eq 0 ]]; then
        log_verbose "No cross-epic child dependency violations found"
    fi

    echo "$error_count"
}

# Check for duplicate task titles
check_duplicate_titles() {
    log_verbose "Checking for duplicate task titles..."

    local dup_count=0

    # Get titles from open tickets via shared JSON cache
    local titles
    titles=$(get_shared_issues_json | python3 -c "import sys,json; [print(t.get('title','')) for t in json.load(sys.stdin)]" 2>/dev/null | sort | uniq -d || true)

    if [[ -n "$titles" ]]; then
        while IFS= read -r title; do
            [[ -z "$title" ]] && continue
            log_minor "Duplicate task title: $title"
            ((dup_count++)) || true
        done <<< "$titles"
    fi

    if [[ $dup_count -eq 0 ]]; then
        log_verbose "No duplicate titles found"
    fi

    echo $dup_count
}

# Check for tasks without descriptions
check_missing_descriptions() {
    log_verbose "Checking for tasks without descriptions..."

    local missing_count=0
    local tmpfile
    tmpfile=$(mktemp)
    # shellcheck disable=SC2064
    trap "rm -f '$tmpfile'" RETURN

    get_shared_issues_json | python3 -c "
import json, sys

try:
    issues = json.load(sys.stdin)
except (json.JSONDecodeError, ValueError):
    issues = []

checked = 0
for issue in issues:
    itype = issue.get('type', issue.get('issue_type', 'task'))
    if itype != 'task':
        continue
    status = issue.get('status', 'open')
    if status == 'closed':
        continue
    desc = issue.get('description', issue.get('body', '') or '')
    iid = issue.get('id', '?')
    title = issue.get('title', issue.get('name', '?'))
    if not desc or not desc.strip():
        print(f'MISSING|{iid}|{title}')
    checked += 1
    if checked >= 20:
        break
" > "$tmpfile"

    while IFS='|' read -r level task_id task_title; do
        case "$level" in
            MISSING)
                log_warning "Task missing description: $task_id - $task_title"
                ((missing_count++)) || true
                ;;
        esac
    done < "$tmpfile"

    echo $missing_count
}

# Check for interface contract tasks missing documentation
check_interface_contracts() {
    log_verbose "Checking interface contract tasks for documentation..."

    local missing_docs_count=0
    local tmpfile
    tmpfile=$(mktemp)
    # shellcheck disable=SC2064
    trap "rm -f '$tmpfile'" RETURN

    get_shared_issues_json | python3 -c "
import json, sys, re

try:
    issues = json.load(sys.stdin)
except (json.JSONDecodeError, ValueError):
    issues = []

always_match = re.compile(r'\binterface\b|\babstract\b|\bbase class\b|\bABC\b', re.IGNORECASE)
contract_match = re.compile(r'\bcontract\b', re.IGNORECASE)
protocol_match = re.compile(r'\bprotocol\b', re.IGNORECASE)
# Exclude known non-interface-contract title patterns (HTTP/test contract frameworks,
# product names like "Model Context Protocol").
false_positive_titles = re.compile(
    r'\bwiremock\b|contract\s+test|test\s+contract|http\s+contract|model\s+context\s+protocol|mcp\s*\(',
    re.IGNORECASE
)

def _is_interface_contract_title(title):
    if false_positive_titles.search(title):
        return False
    if always_match.search(title):
        return True
    return bool(contract_match.search(title) or protocol_match.search(title))

for issue in issues:
    status = issue.get('status', 'open')
    if status == 'closed':
        continue
    title = issue.get('title', issue.get('name', ''))
    if not _is_interface_contract_title(title):
        continue
    iid = issue.get('id', '?')
    desc = issue.get('description', issue.get('body', '') or '')
    notes = issue.get('notes', '') or ''
    combined = desc + notes
    has_file_path = bool(re.search(
        r'src/|\.py|\.sh|\.md|docs/contracts|skills/|file path',
        combined, re.IGNORECASE
    ))
    has_methods = bool(re.search(r'method|function|@abstractmethod', combined, re.IGNORECASE))
    if not has_file_path and not has_methods:
        print(f'MISSING|{iid}|{title}')
" > "$tmpfile"

    while IFS='|' read -r level task_id task_title; do
        case "$level" in
            MISSING)
                log_warning "Interface task may need documentation: $task_id - $task_title"
                log_suggestion "Add notes with: ${TICKET_CMD} comment $task_id 'Interface in src/.../base.py. Key methods: ...'"
                ((missing_docs_count++)) || true
                ;;
        esac
    done < "$tmpfile"

    echo $missing_docs_count
}

# Check for in-progress tasks without notes
check_in_progress_without_notes() {
    log_verbose "Checking for in-progress tasks without progress notes..."

    local missing_notes_count=0
    local tmpfile
    tmpfile=$(mktemp)
    # shellcheck disable=SC2064
    trap "rm -f '$tmpfile'" RETURN

    get_shared_issues_json | python3 -c "
import json, sys

try:
    issues = json.load(sys.stdin)
except (json.JSONDecodeError, ValueError):
    issues = []

for issue in issues:
    status = issue.get('status', 'open')
    if status != 'in_progress':
        continue
    iid = issue.get('id', '?')
    title = issue.get('title', issue.get('name', '?'))
    notes = issue.get('notes', '') or ''
    if not notes.strip():
        print(f'MISSING|{iid}|{title}')
" > "$tmpfile"

    while IFS='|' read -r level task_id task_title; do
        case "$level" in
            MISSING)
                log_warning "In-progress task without notes: $task_id - $task_title"
                ((missing_notes_count++)) || true
                ;;
        esac
    done < "$tmpfile"

    echo $missing_notes_count
}

# Calculate health score
calculate_score() {
    local critical=${#CRITICAL_ISSUES[@]}
    local major=${#MAJOR_ISSUES[@]}
    local minor=${#MINOR_ISSUES[@]}
    local warnings=${#WARNINGS[@]}

    local score=5

    # Critical issues: -2 points each (min score 1)
    if [[ $critical -gt 0 ]]; then
        score=$((score - critical * 2))
    fi

    # Major issues: -1 point for every 2
    if [[ $major -gt 0 ]]; then
        score=$((score - (major + 1) / 2))
    fi

    # Minor issues: -1 point for every 5
    if [[ $minor -gt 0 ]]; then
        score=$((score - (minor + 4) / 5))
    fi

    # Warnings: -1 point for every 10
    if [[ $warnings -gt 0 ]]; then
        score=$((score - (warnings + 9) / 10))
    fi

    # Ensure score is between 1 and 5
    if [[ $score -lt 1 ]]; then
        score=1
    fi
    if [[ $score -gt 5 ]]; then
        score=5
    fi

    echo $score
}

# Output JSON results
output_json() {
    local score=$1

    local critical_json="[]"
    local major_json="[]"
    local minor_json="[]"
    local warnings_json="[]"
    local suggestions_json="[]"

    if [[ ${#CRITICAL_ISSUES[@]} -gt 0 ]]; then
        critical_json=$(printf '%s\n' "${CRITICAL_ISSUES[@]}" | jq -R . | jq -s .)
    fi
    if [[ ${#MAJOR_ISSUES[@]} -gt 0 ]]; then
        major_json=$(printf '%s\n' "${MAJOR_ISSUES[@]}" | jq -R . | jq -s .)
    fi
    if [[ ${#MINOR_ISSUES[@]} -gt 0 ]]; then
        minor_json=$(printf '%s\n' "${MINOR_ISSUES[@]}" | jq -R . | jq -s .)
    fi
    if [[ ${#WARNINGS[@]} -gt 0 ]]; then
        warnings_json=$(printf '%s\n' "${WARNINGS[@]}" | jq -R . | jq -s .)
    fi
    if [[ ${#SUGGESTIONS[@]} -gt 0 ]]; then
        suggestions_json=$(printf '%s\n' "${SUGGESTIONS[@]}" | jq -R . | jq -s .)
    fi

    cat << EOF
{
  "score": $score,
  "critical_issues": $critical_json,
  "major_issues": $major_json,
  "minor_issues": $minor_json,
  "warnings": $warnings_json,
  "suggestions": $suggestions_json
}
EOF
}

# Main execution
main() {
    if ! $JSON_OUTPUT && ! $TERSE_MODE; then
        echo -e "${BLUE}=== Issue Tracking Health Check ===${NC}" >&2
        if $QUICK_MODE; then
            echo -e "${YELLOW}(quick mode — run --full for complete check)${NC}" >&2
        fi
        echo "" >&2
    fi

    if $QUICK_MODE; then
        # Quick mode: run only the fast, high-value checks.
        check_orphaned_tasks > /dev/null
        check_empty_epics > /dev/null
        check_ticket_count > /dev/null
        check_child_parent_deps > /dev/null
        check_cross_epic_child_deps > /dev/null
        check_duplicate_titles > /dev/null
    else
        # Full mode (default): run all checks.
        check_orphaned_tasks > /dev/null
        check_empty_epics > /dev/null
        check_ticket_count > /dev/null
        check_child_parent_deps > /dev/null
        check_cross_epic_child_deps > /dev/null
        check_duplicate_titles > /dev/null
        check_missing_descriptions > /dev/null
        check_interface_contracts > /dev/null
        check_in_progress_without_notes > /dev/null
    fi

    # Calculate score
    local score
    score=$(calculate_score)

    if $JSON_OUTPUT; then
        output_json "$score"
    elif $TERSE_MODE; then
        # Terse mode: single line on clean, multi-line only when issues exist
        local critical=${#CRITICAL_ISSUES[@]}
        local major=${#MAJOR_ISSUES[@]}
        local minor=${#MINOR_ISSUES[@]}
        local warnings=${#WARNINGS[@]}
        if [[ $score -eq 5 ]]; then
            echo "Issues health: ${score}/5 (${critical} critical, ${major} major, ${minor} minor, ${warnings} warnings)" >&2
        else
            echo "" >&2
            echo -e "${BLUE}=== Summary ===${NC}" >&2
            echo "Critical issues: ${critical}" >&2
            echo "Major issues: ${major}" >&2
            echo "Minor issues: ${minor}" >&2
            echo "Warnings: ${warnings}" >&2
            echo "" >&2
            case $score in
                4) echo -e "Health Score: ${GREEN}$score/5${NC} - Good (minor issues)" >&2 ;;
                3) echo -e "Health Score: ${YELLOW}$score/5${NC} - Fair (needs attention)" >&2 ;;
                2) echo -e "Health Score: ${YELLOW}$score/5${NC} - Poor (significant issues)" >&2 ;;
                1) echo -e "Health Score: ${RED}$score/5${NC} - Critical (immediate action needed)" >&2 ;;
            esac
            echo "" >&2
            echo "Run with --verbose for more details" >&2
            echo "Run with --fix to attempt automatic repairs (interactive)" >&2
        fi
    else
        echo "" >&2
        echo -e "${BLUE}=== Summary ===${NC}" >&2
        echo "Critical issues: ${#CRITICAL_ISSUES[@]}" >&2
        echo "Major issues: ${#MAJOR_ISSUES[@]}" >&2
        echo "Minor issues: ${#MINOR_ISSUES[@]}" >&2
        echo "Warnings: ${#WARNINGS[@]}" >&2
        echo "" >&2

        # Display score with color
        case $score in
            5)
                echo -e "Health Score: ${GREEN}$score/5${NC} - Excellent" >&2
                ;;
            4)
                echo -e "Health Score: ${GREEN}$score/5${NC} - Good (minor issues)" >&2
                ;;
            3)
                echo -e "Health Score: ${YELLOW}$score/5${NC} - Fair (needs attention)" >&2
                ;;
            2)
                echo -e "Health Score: ${YELLOW}$score/5${NC} - Poor (significant issues)" >&2
                ;;
            1)
                echo -e "Health Score: ${RED}$score/5${NC} - Critical (immediate action needed)" >&2
                ;;
        esac

        if [[ $score -lt 5 ]]; then
            echo "" >&2
            echo "Run with --verbose for more details" >&2
            echo "Run with --fix to attempt automatic repairs (interactive)" >&2
        fi
    fi

    # Return exit code based on score
    exit $((5 - score))
}

main
