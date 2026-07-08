#!/usr/bin/env bash
# rebar session-provenance capture shim — Claude Code SessionStart hook (story ec5c).
#
# External-API contract (Claude Code hooks reference: https://code.claude.com/docs/en/hooks):
# a SessionStart hook receives a JSON envelope on stdin with a `session_id` field, and may
# export env vars into the session by APPENDING `export KEY=value` lines to the file named by
# the `CLAUDE_ENV_FILE` env var. If a future Claude Code changes those names/mechanism, this
# shim simply no-ops (no CLAUDE_ENV_FILE / unparsed session_id -> exit 0) — it never errors.
#
# Reads the SessionStart JSON envelope from stdin (field `session_id`) and exports it as the
# portable REBAR_SESSION_ID, plus the harness tag AI_AGENT=claude-code, so a local Claude Code
# CLI claim records `claimed_session` / `claim_harness` (the CLAUDE_CODE_SESSION_ID env var is
# exposed ONLY in the remote runner). Wire it up via .claude/settings.json:
#
#   { "hooks": { "SessionStart": [ { "matcher": "startup|resume|clear|compact",
#       "hooks": [ { "type": "command",
#                    "command": "scripts/session-shims/claude-code-session-start.sh" } ] } ] } }
#
# Fire-and-forget: always exits 0 and never blocks the session. Idempotent — re-firing on
# resume/clear/compact first STRIPS this hook's own prior exports from the env file, then
# re-writes the current session's values, so the file never accumulates duplicate/conflicting
# lines and always reflects the current session (session ids are not stable across resume, per
# gotcha G1). Other hooks' lines are preserved. Session-scoped by design (writes only to
# $CLAUDE_ENV_FILE — never a shell profile; see docs/session-id-shims.md).
set -u

# The env-injection file Claude Code provides to SessionStart hooks. No file -> nothing to do.
[ -n "${CLAUDE_ENV_FILE:-}" ] || exit 0

# Parse session_id from the stdin JSON with python3 (the interpreter rebar itself runs on — a
# system prerequisite, `requires-python >= 3.11`). If python3 is somehow absent or the parse
# fails, this yields an empty string and the hook no-ops below (fail-safe).
session_id="$(python3 -c 'import json,sys
try:
    d = json.load(sys.stdin)
    v = d.get("session_id") or ""
except Exception:
    v = ""
print(v.strip() if isinstance(v, str) else "")' 2>/dev/null)"

# Nothing usable -> silent no-op (still exit 0).
[ -n "$session_id" ] || exit 0

# Harness-provenance tag: the base name from the AI_AGENT vocabulary (docs/config.md). A
# `_<version>` suffix is permitted by that vocabulary but the local hook has no reliable
# version source, so it emits the bare base name.
harness="claude-code"

# Idempotency: drop this hook's OWN prior exports (preserving every other line) before
# re-appending, so re-firing on resume/compact neither accumulates duplicate lines nor leaves a
# stale id — the current session's value wins. Then append with >> (other hooks' vars survive).
# %q quotes each value so it is an opaque literal, never re-interpreted by the sourcing shell.
# Every write below is best-effort with failures suppressed: a read-only file, a missing
# parent dir, or any I/O error must NEVER produce a non-zero exit that could block session
# startup (the script always ends at `exit 0`).
if [ -s "$CLAUDE_ENV_FILE" ]; then
    _tmp="${CLAUDE_ENV_FILE}.rebar.$$"
    # Keep every line EXCEPT this hook's own prior exports (an empty result truncates via mv).
    grep -vE '^export (REBAR_SESSION_ID|AI_AGENT)=' "$CLAUDE_ENV_FILE" >"$_tmp" 2>/dev/null
    mv "$_tmp" "$CLAUDE_ENV_FILE" 2>/dev/null || rm -f "$_tmp" 2>/dev/null
fi
{
    printf "export REBAR_SESSION_ID=%q\n" "$session_id"
    printf "export AI_AGENT=%q\n" "$harness"
} >>"$CLAUDE_ENV_FILE" 2>/dev/null || true

exit 0
