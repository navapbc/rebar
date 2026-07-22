#!/usr/bin/env bash
# normalize_ci_conclusion.sh — map the aggregated CI conclusion into the Gerrit
# review action's vote-type domain (ticket bad2).
#
# WHY: the Verified vote job aggregates this run's jobs with
# im-open/workflow-conclusion, which sets WORKFLOW_CONCLUSION to one of
#   {success, failure, cancelled, skipped}
# where `skipped` is its FALLBACK value when none of success/failure/cancelled is
# observed. That value used to be piped VERBATIM into
# lfreleng-actions/gerrit-review-action's `vote-type`, whose accepted domain is ONLY
#   {clear, success, failure, cancelled}.
# On `skipped` the action hit its default branch (`::error::Unknown vote-type ...;
# exit 1`) BEFORE posting any label, so a green-CI change received NO Verified vote
# and was un-landable (violating AGENTS.md:148,150 / src/rebar/_guides/passing-code-review.md:
# green CI must yield Verified +1, failed CI Verified -1 — posting neither is a bug).
#
# This script closes that domain mismatch: given the raw conclusion and a signal of
# whether any needed job actually failed/cancelled, it emits a vote-type that is
# ALWAYS in {success, failure, cancelled}.
#
# Inputs (env, so the workflow step maps GitHub contexts onto them):
#   CONCLUSION        raw WORKFLOW_CONCLUSION (may be empty)
#   FAILURE_OBSERVED  "true" if any needed job's result was failure/cancelled
# Output (stdout): exactly one of success|failure|cancelled
#
# Mapping:
#   success                 -> success
#   failure                 -> failure
#   cancelled               -> cancelled
#   skipped & no failure    -> success   (benign fallback: nothing failed)
#   skipped & failure obs   -> failure   (fail-closed: a real failure was seen)
#   empty / anything else   -> failure   (fail-closed anomaly; never out-of-domain)
set -euo pipefail

conclusion="${CONCLUSION:-}"
failure_observed="${FAILURE_OBSERVED:-false}"

case "$conclusion" in
  success)
    vote="success"
    ;;
  failure)
    vote="failure"
    ;;
  cancelled)
    vote="cancelled"
    ;;
  skipped)
    # im-open's fallback: nothing failed/cancelled -> the run is green; but if a
    # needed job DID fail/cancel, fail closed rather than trusting the fallback.
    if [ "$failure_observed" = "true" ]; then
      vote="failure"
    else
      vote="success"
    fi
    ;;
  *)
    # Empty or any unrecognized value is an anomaly: fail closed so we never emit
    # an out-of-domain vote-type (the exact bug this script exists to prevent).
    vote="failure"
    ;;
esac

printf '%s\n' "$vote"
