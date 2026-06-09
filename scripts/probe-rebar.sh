#!/usr/bin/env bash
# scripts/probe-rebar.sh
#
# Reusable end-to-end PROBE for the rebar ticket system. Exercises every CLI
# command and a broad set of edge cases against the REAL rebar engine, asserting
# exit codes and output invariants, then prints a PASS/FAIL summary.
#
# SAFETY: by default the probe runs in an ISOLATED temporary tracker (its own
# REBAR_ROOT), so it never touches this project's real tickets and is safe to run
# repeatedly. It still drives the project's installed `rebar` (the live engine).
# Set PROBE_LIVE=1 to instead exercise the project's real store — in that mode the
# probe snapshots the existing ticket set, only removes the tickets it creates,
# and verifies the store is unchanged at the end.
#
# Usage:
#   bash scripts/probe-rebar.sh                # isolated tracker (recommended)
#   REBAR=/path/to/rebar bash scripts/probe-rebar.sh
#   PROBE_LIVE=1 bash scripts/probe-rebar.sh   # against the real project store
#
# Exit: 0 if all checks pass, 1 otherwise.

set -uo pipefail

# ── rebar command resolution ────────────────────────────────────────────────
# Prefer an explicit $REBAR, else the repo's editable .venv, else PATH.
_here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_repo_root="$(cd "$_here/.." && pwd)"
if [ -n "${REBAR:-}" ]; then
    RB=$REBAR
elif [ -x "$_repo_root/.venv/bin/rebar" ]; then
    RB="$_repo_root/.venv/bin/rebar"
else
    RB="rebar"
fi
command -v "$RB" >/dev/null 2>&1 || { echo "FATAL: rebar not found ($RB)"; exit 1; }
for bin in jq git python3; do
    command -v "$bin" >/dev/null 2>&1 || { echo "FATAL: missing dependency: $bin"; exit 1; }
done

PASS=0; FAIL=0
_fail() { FAIL=$((FAIL+1)); printf '  \033[31mFAIL\033[0m: %s\n' "$*" >&2; }
_pass() { PASS=$((PASS+1)); }
section() { printf '\n=== %s ===\n' "$*"; }

# ── assertion helpers ────────────────────────────────────────────────────────
# Run a command, capture stdout+stderr and exit code into globals OUT / RC.
run() { OUT="$("$@" 2>&1)"; RC=$?; }
run_rb() { run "$RB" "$@"; }

assert_rc() { # <expected_rc> <label>
    if [ "$RC" -eq "$1" ]; then _pass; else _fail "$2 (exit $RC, want $1)\n    out: ${OUT:0:300}"; fi
}
assert_rc_ne() { # <not_expected_rc> <label>
    if [ "$RC" -ne "$1" ]; then _pass; else _fail "$2 (exit $RC, want != $1)"; fi
}
assert_contains() { # <needle> <label>
    case "$OUT" in *"$1"*) _pass;; *) _fail "$2 (missing '$1' in: ${OUT:0:300})";; esac
}
assert_not_contains() { # <needle> <label>
    case "$OUT" in *"$1"*) _fail "$2 (unexpected '$1')";; *) _pass;; esac
}
assert_eq() { # <expected> <actual> <label>
    if [ "$1" = "$2" ]; then _pass; else _fail "$3 (got '$2', want '$1')"; fi
}

# Extract the created ticket id (last non-empty line of `create` output).
_last_id() { printf '%s\n' "$OUT" | grep -E '^[0-9a-f]{4}-' | tail -1; }

# ── environment setup ────────────────────────────────────────────────────────
_CLEAN_DIRS=()
if [ "${PROBE_LIVE:-}" = "1" ]; then
    MODE="LIVE (real project store)"
    # Snapshot the existing ticket dir set so we only remove what we create.
    _root="${REBAR_ROOT:-${PROJECT_ROOT:-$(git -C "$_repo_root" rev-parse --show-toplevel)}}"
    export REBAR_ROOT="$_root"
    TRACKER="$REBAR_ROOT/.tickets-tracker"
    _PRE_IDS="$(ls -1 "$TRACKER" 2>/dev/null | grep -E '^[0-9a-f]{4}-' | sort || true)"
else
    MODE="ISOLATED (temp tracker)"
    _tmp="$(mktemp -d)"; _CLEAN_DIRS+=("$_tmp")
    export REBAR_ROOT="$_tmp/repo"
    mkdir -p "$REBAR_ROOT"
    ( cd "$REBAR_ROOT" && git init -q && git config user.email probe@example.com \
        && git config user.name probe && git commit -q --allow-empty -m init )
    TRACKER="$REBAR_ROOT/.tickets-tracker"
    _PRE_IDS=""
fi
# Skip the network sync in isolated/probe runs (no remote).
export _TICKET_TEST_NO_SYNC=1

# Track ids we create so we can clean them up precisely.
_CREATED=()
# mk <VARNAME> <type> <title> [extra args...]
# Runs `create` in the CURRENT shell (so OUT/RC propagate for assertions),
# assigns the new id to VARNAME, and records it for cleanup.
mk() {
    local __var="$1"; shift
    run_rb create "$@"
    local __id; __id="$(_last_id)"
    [ -n "$__id" ] && _CREATED+=("$__id")
    printf -v "$__var" '%s' "$__id"
}

cleanup() {
    # Remove only the tickets this probe created; leave pre-existing ones intact.
    if [ "${#_CREATED[@]}" -gt 0 ] && [ -d "$TRACKER" ]; then
        ( cd "$TRACKER" 2>/dev/null \
            && git rm -r --quiet "${_CREATED[@]}" 2>/dev/null
          rm -rf "${_CREATED[@]}" .graph-cache.json 2>/dev/null
          git commit --quiet -m "probe cleanup" 2>/dev/null ) || true
    fi
    if [ "${PROBE_LIVE:-}" = "1" ]; then
        local post; post="$(ls -1 "$TRACKER" 2>/dev/null | grep -E '^[0-9a-f]{4}-' | sort || true)"
        if [ "$post" = "$_PRE_IDS" ]; then
            printf '\nLIVE store verified unchanged after cleanup.\n'
        else
            printf '\n\033[31mWARNING: live store differs after cleanup!\033[0m\n' >&2
            diff <(printf '%s' "$_PRE_IDS") <(printf '%s' "$post") >&2 || true
        fi
    fi
    for d in "${_CLEAN_DIRS[@]:-}"; do rm -rf "$d" 2>/dev/null || true; done
}
trap cleanup EXIT

echo "rebar probe — mode: $MODE — rebar: $RB"

# ════════════════════════════════════════════════════════════════════════════
section "create — types, fields, and validation"
mk EPIC epic "PROBE: epic" --priority 1 --assignee alice --tags probe,top; assert_rc 0 "create epic"
mk STORY story "PROBE: story" --parent "$EPIC" --tags probe; assert_rc 0 "create story --parent"
mk TASK task "PROBE: task with all fields" --priority 0 --assignee bob \
    --description $'Body line.\n\n## Acceptance Criteria\n- [ ] a\n- [ ] b' --tags probe,alpha; assert_rc 0 "create task full"
mk BUG bug "PROBE: bug"; assert_rc 0 "create bug minimal"
run_rb create widget "PROBE: bad type"; assert_rc_ne 0 "create rejects invalid type"; assert_contains "invalid ticket type" "invalid-type message"
run_rb create task "PROBE: bad pri" --priority 9; assert_rc_ne 0 "create rejects priority>4"

section "show — alias, short id, missing, fields"
run_rb show "$TASK"; assert_rc 0 "show by id"; assert_contains '"priority": 0' "show priority field"
ALIAS=$("$RB" show "$TASK" | jq -r .alias)
run_rb show "$ALIAS"; assert_rc 0 "show by alias"
run_rb show "${TASK:0:4}"; assert_rc 0 "show by short (4-hex) id"
run_rb show no-such-ticket-xyz; assert_rc_ne 0 "show missing -> non-zero"; assert_contains '"error": "ticket_not_found"' "show missing JSON envelope"

section "edit — each field + validation"
run_rb edit "$BUG" --title="PROBE: edited" --priority=3 --assignee=carol --description="d" --tags=probe,edited; assert_rc 0 "edit multi-field"
assert_eq 3 "$("$RB" show "$BUG" | jq .priority)" "edit priority persisted"
run_rb edit "$BUG" --priority=99; assert_rc_ne 0 "edit rejects priority>4"
run_rb edit "$BUG" --priority=high; assert_rc_ne 0 "edit rejects non-numeric priority"
run_rb edit "$BUG" --ticket_type=widget; assert_rc_ne 0 "edit rejects invalid ticket_type"
run_rb edit "$BUG" --ticket_type=task; assert_rc 0 "edit valid ticket_type"

section "tags — add, idempotent, untag (missing graceful)"
run_rb tag "$STORY" urgent; assert_rc 0 "tag add"
run_rb tag "$STORY" urgent; assert_rc 0 "tag idempotent"
assert_eq 1 "$("$RB" show "$STORY" | jq '[.tags[]|select(.=="urgent")]|length')" "tag appears once"
run_rb untag "$STORY" urgent; assert_rc 0 "untag"
run_rb untag "$STORY" nonexistent; assert_rc 0 "untag missing graceful"
assert_eq 1 "$("$RB" list --has-tag=alpha | jq length)" "list --has-tag filter"

section "links — relations, cycle, self, invalid, unlink"
# Exercise each relation on the same pair, clearing between iterations so the
# inverse blocking relations (blocks/depends_on) don't legitimately form a cycle.
for rel in blocks depends_on relates_to duplicates supersedes discovered_from; do
    run_rb link "$TASK" "$BUG" "$rel"; assert_rc 0 "link $rel"
    "$RB" unlink "$TASK" "$BUG" >/dev/null 2>&1 || true
done
run_rb link "$TASK" "$BUG" blocks; assert_rc 0 "link blocks (set up cycle test)"
run_rb link "$BUG" "$TASK" blocks; assert_rc_ne 0 "cycle rejected"; assert_contains "cycle" "cycle message"
run_rb link "$TASK" "$TASK" blocks; assert_rc_ne 0 "self-link rejected"
run_rb link "$TASK" "$BUG" frobnicate; assert_rc_ne 0 "invalid relation rejected"
run_rb link "$TASK" "$BUG"; assert_rc_ne 0 "link requires a relation"
run_rb deps "$TASK"; assert_rc 0 "deps"; assert_contains '"ready_to_work"' "deps shape"
run_rb unlink "$TASK" "$BUG"; assert_rc 0 "unlink (pair-scoped)"

section "claim + optimistic-concurrency (exit 10)"
run_rb claim "$TASK" --assignee dave; assert_rc 0 "claim open ticket"
assert_eq in_progress "$("$RB" show "$TASK" | jq -r .status)" "claim -> in_progress"
run_rb claim "$TASK" --assignee eve; assert_rc 10 "double-claim -> exit 10"
run_rb transition "$TASK" open closed; assert_rc 10 "stale current_status -> exit 10"

section "transition — blocked, auto-detect, backward, task close, reopen"
run_rb transition "$TASK" in_progress blocked; assert_rc 0 "-> blocked"
run_rb transition "$TASK" blocked in_progress; assert_rc 0 "blocked -> in_progress"
run_rb transition "$TASK" open; assert_rc 0 "2-arg auto-detect (in_progress -> open)"
assert_eq open "$("$RB" show "$TASK" | jq -r .status)" "auto-detect landed on open"
run_rb transition "$TASK" open in_progress; assert_rc 0 "open -> in_progress"
run_rb transition "$TASK" in_progress open; assert_rc 0 "in_progress -> open (backward)"
run_rb transition "$TASK" open closed; assert_rc 0 "task close (no reason needed)"
run_rb reopen "$TASK"; assert_rc 0 "reopen closed -> open"
assert_eq open "$("$RB" show "$TASK" | jq -r .status)" "reopen status"

section "close guards — bug reason prefix, story/epic verdict-hash"
mk RBUG bug "PROBE: reason-guard bug"; assert_rc 0 "create fresh bug"
run_rb transition "$RBUG" open closed; assert_rc_ne 0 "bug close requires --reason"
run_rb transition "$RBUG" open closed --reason="patched"; assert_rc_ne 0 "bug reason prefix enforced"
run_rb transition "$RBUG" open closed --reason="Fixed: probe"; assert_rc 0 "bug close with Fixed: reason"
run_rb transition "$STORY" open closed; assert_rc_ne 0 "story close requires --verdict-hash"
run_rb transition "$STORY" open closed --force-close="probe"; assert_rc 0 "story --force-close bypasses verdict-hash"
# EPIC's only child (STORY) is now closed, so the children guard allows the close.
run_rb transition "$EPIC" open closed --force-close="probe"; assert_rc 0 "epic --force-close (child already closed)"

section "quality gates"
run_rb clarity-check "$TASK"; assert_contains '"score"' "clarity-check JSON"
run_rb check-ac "$TASK"; assert_rc 0 "check-ac pass (AC present)"
run_rb check-ac "$BUG"; assert_rc_ne 0 "check-ac fail (no AC)"
run_rb quality-check "$TASK"; assert_contains "QUALITY:" "quality-check"
run_rb validate --json; assert_rc_ne 10 "validate repo-wide --json runs"; assert_contains '"score"' "validate report"
run_rb validate "$TASK"; assert_rc_ne 0 "validate rejects a ticket id (repo-wide)"

section "file-impact / verify-commands (+ invalid JSON)"
run_rb set-file-impact "$TASK" '[{"path":"a.py","reason":"r"}]'; assert_rc 0 "set-file-impact"
run_rb get-file-impact "$TASK"; assert_contains '"a.py"' "get-file-impact"
run_rb set-file-impact "$TASK" 'not-json'; assert_rc_ne 0 "set-file-impact rejects bad JSON"
run_rb set-verify-commands "$TASK" '[{"dd_id":"D1","dd_text":"t","command":"echo"}]'; assert_rc 0 "set-verify-commands"
run_rb get-verify-commands "$TASK"; assert_contains '"D1"' "get-verify-commands"

section "scratch set/get/clear"
run_rb scratch set "$TASK" k "v"; assert_rc 0 "scratch set"
run_rb scratch get "$TASK" k; assert_contains '"hit"' "scratch get hit"
run_rb scratch clear "$TASK" k; assert_rc 0 "scratch clear"
run_rb scratch get "$TASK" k; assert_contains '"miss"' "scratch get miss after clear"

section "reads — search, ready, next-batch, summary, exists, epics, descendants"
run_rb search PROBE; assert_rc 0 "search"; assert_contains '"ticket_id"' "search returns states"
run_rb ready; assert_rc 0 "ready (default id-list)"
run_rb ready --json; assert_rc 0 "ready --json"; assert_eq array "$(printf '%s' "$OUT" | jq -r 'type')" "ready --json is an array"
run_rb summary "$TASK"; assert_rc 0 "summary"; assert_not_contains "[unknown]" "summary renders status"
run_rb exists "$TASK"; assert_rc 0 "exists by id"
run_rb exists "$ALIAS"; assert_rc 0 "exists by alias"
run_rb exists no-such-xyz; assert_rc_ne 0 "exists absent -> non-zero"
# Fresh OPEN epic + child for epic-scoped reads (the earlier EPIC was closed above).
mk EPIC2 epic "PROBE: open epic"; mk CHILD2 task "PROBE: batch child" --parent "$EPIC2"
run_rb list-epics; assert_rc 0 "list-epics (open epic present)"
run_rb next-batch "$EPIC2"; assert_rc 0 "next-batch text"
run_rb next-batch "$EPIC2" --json; assert_rc 0 "next-batch --json"; assert_contains '"batch"' "next-batch JSON shape"
run_rb list-descendants "$EPIC2"; assert_rc 0 "list-descendants"; assert_contains '"stories"' "descendants shape"

section "single-reducer parity — show == list == search shape (bug f026)"
SK="$("$RB" show "$TASK" | jq -S 'keys')"
LK="$("$RB" list | jq -S --arg t "$TASK" '.[]|select(.ticket_id==$t)|keys')"
assert_eq "$SK" "$LK" "show and list element have identical key sets"
run_rb show "$TASK"; assert_not_contains '"parent_status_uuid"' "internal key not leaked in show"
assert_eq '[{"command":"echo","dd_id":"D1","dd_text":"t"}]' \
    "$("$RB" list | jq -c --arg t "$TASK" '.[]|select(.ticket_id==$t)|.verify_commands')" \
    "verify_commands visible in list (single-reducer)"

section "--help — usage without execution; free-text not intercepted"
_before=$("$RB" list | jq length)
run_rb init --help; assert_rc 0 "init --help exits 0"; assert_contains "Usage: rebar init" "init --help shows usage"; assert_not_contains "initialized" "init --help did not run"
run_rb create --help; assert_rc 0 "create --help"; assert_contains "Usage: rebar create" "create --help usage"
assert_eq "$_before" "$("$RB" list | jq length)" "create --help created nothing"
run_rb --help; assert_rc 0 "top-level --help"; assert_contains "Subcommands:" "overview"
mk FT task "PROBE: title with --help inside"; assert_rc 0 "free-text --help not intercepted (ticket created)"
run_rb show "$FT"; assert_contains "--help" "free-text --help preserved in title"

section "archive + fsck + compact"
mk ARCH task "PROBE: to archive"
run_rb archive "$ARCH"; assert_rc 0 "archive"
assert_eq archived "$("$RB" show "$ARCH" | jq -r .status)" "archived status"
run_rb archive "$ARCH"; assert_rc 0 "archive idempotent"
run_rb compact "$TASK"; assert_rc 0 "compact"
run_rb fsck; assert_rc 0 "fsck clean"; assert_contains "fsck complete" "fsck summary"

section "delete — friction (requires --user-approved)"
mk DEL task "PROBE: to delete"
run_rb delete "$DEL"; assert_rc_ne 0 "delete without --user-approved refused"
run_rb delete "$DEL" --user-approved; assert_rc 0 "delete --user-approved"

# ════════════════════════════════════════════════════════════════════════════
printf '\n────────────────────────────────────────\n'
printf 'PROBE RESULT: %d passed, %d failed (mode: %s)\n' "$PASS" "$FAIL" "$MODE"
[ "$FAIL" -eq 0 ]
