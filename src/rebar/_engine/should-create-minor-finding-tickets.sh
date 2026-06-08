#!/usr/bin/env bash
set -uo pipefail
# scripts/should-create-minor-finding-tickets.sh
#
# Gate script consulted by REVIEW-WORKFLOW.md before auto-filing minor /
# suggestion-class findings as bug tickets. Default is fail-closed (do NOT
# create tickets) — the deferred-nitpick treadmill (bugs 57b9, 9726, 5329)
# showed that auto-filed minor findings sit at pri=4 indefinitely and burn
# triage cost without ever being acted on. Surface as PR comments instead.
#
# Opt-in by setting `review.minor_findings_create_tickets=true` in
# .claude/dso-config.conf.
#
# Usage:
#   if "${CLAUDE_PLUGIN_ROOT}/scripts/should-create-minor-finding-tickets.sh"; then
#       # auto-file minor findings as bug tickets
#   fi
#
# Exit codes:
#   0 — config explicitly enables minor-finding ticket creation
#   1 — disabled (default), explicitly false, or unrecognized value (fail-closed)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

VALUE=$(bash "$SCRIPT_DIR/read-config.sh" review.minor_findings_create_tickets 2>/dev/null || true)

# Normalize case: read-config.sh emits "True"/"False" (Python str(bool)) for
# YAML-format configs and "true"/"false" for .conf-format. Compare in lowercase
# so the gate behaves identically across both formats.
if [[ "${VALUE,,}" == "true" ]]; then
    exit 0
fi
exit 1
