# Jira onboarding reference (groundwork)

Reference material for the planned **Jira onboarding & configuration** flow for
rebar. Kept here so the existing, working building blocks aren't lost, while
staying out of the shipped product until the feature is actually wired up.

**Not wired into the `rebar` dispatcher and not shipped in the published wheel.**
These files live under `scripts/` (outside the wheel's `packages = ["src/rebar"]`)
and outside `tests/`, so `pytest` never collects them.

## Files

- `jira-credential-helper.sh` — detects the Jira credential environment variables
  (`JIRA_URL`, `JIRA_USER`, `JIRA_API_TOKEN`) and emits structured
  `DETECTED=` / `MISSING=` / `GUIDANCE_*` output, plus a `CONFIRM_BEFORE_COPY`
  signal when a token is present and `JIRA_PROJECT=KEY` when `--project=KEY` is
  passed. Informational only (always exits 0). The intended seed for an
  interactive `rebar` onboarding/credentials-preflight command.
- `test-jira-credential-helper.sh` — behavioral tests for the helper (env-stub
  based, no network). Run it manually:

  ```bash
  bash scripts/jira-onboarding-reference/test-jira-credential-helper.sh
  ```

When the onboarding feature is built, promote the helper back into
`src/rebar/_engine/`, wire a dispatcher arm, and move its test into `tests/`.
See the rebar tracker for the follow-up ticket.
