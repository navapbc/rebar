#!/usr/bin/env bash
# Verifies that the GHA runner is accessible and logs the result as a story comment.
#
# Usage:
#   STORY_ID=<ticket-id> WORKFLOW_NAME=<workflow-file> bash verify-gha-runner.sh
#
# Environment variables:
#   STORY_ID      — ticket ID to post the comment to (default: 7705-41e8-9f01-4ebb)
#   WORKFLOW_NAME — workflow file name to query (default: reconcile-bridge.yml)

set -euo pipefail

STORY_ID="${STORY_ID:-7705-41e8-9f01-4ebb}"
WORKFLOW_NAME="${WORKFLOW_NAME:-reconcile-bridge.yml}"
GH_TIMEOUT="${GH_TIMEOUT:-30s}"

# Resolve repo root so we can invoke the repo-pinned rebar CLI rather than the
# bare `dso` lookup (PATH may resolve the wrong binary in CI/dev shells).
_REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"
DSO_CMD="${DSO_CMD:-${REBAR_TICKET_CLI:-$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)/rebar}}"
if [[ ! -x "$DSO_CMD" ]]; then
    DSO_CMD="dso"  # fall back to PATH (e.g., outside a checkout)
fi

# Portable timeout resolver. GNU `timeout` ships in Linux coreutils but not in
# stock macOS — coreutils via Homebrew installs it as `gtimeout`. Fall back to
# Python's signal-based timer if neither is on PATH (works on any system that
# has python3, which the rest of dso already assumes).
if command -v timeout >/dev/null 2>&1; then
    _run_with_timeout() { timeout "$@"; }
elif command -v gtimeout >/dev/null 2>&1; then
    _run_with_timeout() { gtimeout "$@"; }
else
    # Pure-python timer: convert "30s"/"30" → seconds float, spawn the
    # command via subprocess.run(timeout=...) which sends SIGKILL on expiry
    # (not SIGTERM — Python's subprocess.run timeout uses Popen.kill() under
    # the hood), then exit 124 to match GNU `timeout`'s exit convention.
    # This is a coarser cleanup than GNU timeout's SIGTERM-then-grace-period
    # but is sufficient for non-interactive gh CLI calls.
    _run_with_timeout() {
        local _spec="$1"; shift
        python3 - "$_spec" "$@" <<'PYEOF'
import re
import subprocess
import sys

spec = sys.argv[1]
args = sys.argv[2:]
# Strict numeric format: digits with optional single decimal, then optional unit.
# Rejects "..5s", "1.2.3", ".s", etc. — those raise on the regex check below
# rather than crashing float() with an uncaught ValueError.
m = re.match(r"^([0-9]+(?:\.[0-9]+)?)([smh]?)$", spec)
if not m:
    sys.stderr.write(f"_run_with_timeout: bad spec {spec!r}\n")
    sys.exit(125)
try:
    n = float(m.group(1))
except ValueError:
    sys.stderr.write(f"_run_with_timeout: bad numeric in spec {spec!r}\n")
    sys.exit(125)
unit = m.group(2) or "s"
seconds = n * {"s": 1, "m": 60, "h": 3600}[unit]
if not args:
    sys.stderr.write("_run_with_timeout: no command given\n")
    sys.exit(125)
try:
    proc = subprocess.run(args, timeout=seconds)
    sys.exit(proc.returncode)
except subprocess.TimeoutExpired:
    sys.exit(124)  # matches GNU `timeout` exit convention
except FileNotFoundError:
    sys.stderr.write(f"_run_with_timeout: command not found: {args[0]!r}\n")
    sys.exit(127)  # matches `command not found` shell convention
PYEOF
    }
fi

# Step 1: Verify gh authentication (with timeout to avoid hanging on slow/blocked CLI state)
if ! _run_with_timeout "$GH_TIMEOUT" gh auth status >/dev/null 2>&1; then
    echo "ERROR: gh auth failed (or timed out after $GH_TIMEOUT)" >&2
    exit 1
fi

# Step 2: Run gh run list and capture combined output (with timeout)
_output=""
if ! _output=$(_run_with_timeout "$GH_TIMEOUT" gh run list --workflow="$WORKFLOW_NAME" --limit 1 2>&1); then
    echo "ERROR: gh run list failed (or timed out after $GH_TIMEOUT)" >&2
    exit 1
fi

# Step 3: Log the output as a story comment via the repo-pinned rebar CLI
"$DSO_CMD" ticket comment "$STORY_ID" "GHA runner verified: $(echo "$_output" | head -5)"

exit 0
