#!/usr/bin/env bash
# PATH-shim mock for sub-agent Task dispatch
# Used by cascade-e2e tests. Does NOT dispatch real agents.
# Requires CASCADE_E2E_LOG env var to be set to a writable directory.
# Accepts a JSON payload via stdin or args and logs to $CASCADE_E2E_LOG/mock-dispatch-calls.log.
set -euo pipefail

if [[ -z "${CASCADE_E2E_LOG:-}" ]]; then
  printf 'mock-task-dispatch.sh: CASCADE_E2E_LOG is unset or empty — aborting\n' >&2
  exit 1
fi

LOG_FILE="${CASCADE_E2E_LOG}/mock-dispatch-calls.log"

# Read payload from stdin if available, otherwise use args
if [[ -t 0 ]]; then
  PAYLOAD="${*:-}"
else
  PAYLOAD="$(cat)"
fi

TIMESTAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
printf '%s\n' "{\"action\":\"dispatch\",\"timestamp\":\"${TIMESTAMP}\",\"payload\":${PAYLOAD:-null}}" >> "${LOG_FILE}"

# Return a stub response
printf '%s\n' '{"status":"pass","output":"mock-output"}'

exit 0
