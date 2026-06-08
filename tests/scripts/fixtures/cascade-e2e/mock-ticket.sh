#!/usr/bin/env bash
# PATH-shim mock for .claude/scripts/dso ticket
# Used by cascade-e2e tests. Does NOT modify real tickets.
# Requires CASCADE_E2E_LOG env var to be set to a writable directory.
set -euo pipefail

if [[ -z "${CASCADE_E2E_LOG:-}" ]]; then
  printf 'mock-ticket.sh: CASCADE_E2E_LOG is unset or empty — aborting\n' >&2
  exit 1
fi

LOG_FILE="${CASCADE_E2E_LOG}/mock-ticket-calls.log"

SUBCOMMAND="${1:-}"
shift || true

case "${SUBCOMMAND}" in
  comment)
    TICKET_ID="${1:-}"
    COMMENT_TEXT="${2:-}"
    printf '%s\n' "{\"action\":\"comment\",\"id\":\"${TICKET_ID}\",\"text\":\"${COMMENT_TEXT}\"}" >> "${LOG_FILE}"
    ;;
  show)
    TICKET_ID="${1:-}"
    printf '%s\n' "{\"ticket_id\":\"${TICKET_ID}\",\"status\":\"in_progress\",\"title\":\"mock\"}"
    printf '%s\n' "{\"action\":\"show\",\"id\":\"${TICKET_ID}\"}" >> "${LOG_FILE}"
    ;;
  list)
    printf '%s\n' "[]"
    printf '%s\n' "{\"action\":\"list\",\"args\":\"$*\"}" >> "${LOG_FILE}"
    ;;
  transition)
    printf '%s\n' "{\"action\":\"transition\",\"args\":\"$*\"}" >> "${LOG_FILE}"
    ;;
  *)
    printf '%s\n' "{\"action\":\"${SUBCOMMAND}\",\"args\":\"$*\"}" >> "${LOG_FILE}"
    ;;
esac

exit 0
