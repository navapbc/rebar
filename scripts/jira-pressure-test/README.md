# Jira pressure-test probes

These maintained end-to-end probes exercise the Jira reconciler against a connected Jira instance. Use them when validating bridge changes that need evidence beyond the hermetic contract suite.

The probes are not part of the automated test suite and are not included in the published wheel. Run them manually outside CI because they create, edit, and delete Jira issues and local tickets.

## Scripts

- `e2e_validation_probe.sh` exercises the bidirectional sync pipeline for one ticket. It requires `REBAR_E2E_VALIDATION_PROBE=1`.
- `e2e_field_validation_probe.sh` exercises bidirectional field operations across ten tickets. It requires `REBAR_FIELD_VALIDATION_PROBE=1`.

Each probe exits with a nonzero status when its opt-in is absent. The separate variables prevent one probe authorization from enabling the other.

## Preflight

Each probe validates the following requirements before it invokes the ticket CLI, runs the reconciler, or mutates Jira:

- `JIRA_URL`, `JIRA_USER`, `JIRA_API_TOKEN`, and `JIRA_PROJECT` are set. No project is selected by default.
- The checkout Python at `.venv/bin/python` is executable.
- The checkout ticket command at `.venv/bin/rebar` is executable unless `REBAR_TICKET_CLI` selects another executable.
- The engine directory at `src/rebar/_engine` exists unless `REBAR_ENGINE_DIR` selects another directory.
- The checkout Python can import `rebar_reconciler` from the selected engine directory.

## Run the probes

Run these commands manually from the repository root after provisioning the checkout with `make install`:

```bash
export JIRA_URL=... JIRA_USER=... JIRA_API_TOKEN=...
export JIRA_PROJECT=REB

REBAR_E2E_VALIDATION_PROBE=1 \
  bash scripts/jira-pressure-test/e2e_validation_probe.sh

REBAR_FIELD_VALIDATION_PROBE=1 \
  bash scripts/jira-pressure-test/e2e_field_validation_probe.sh
```

Set `REBAR_ENGINE_DIR` or `REBAR_TICKET_CLI` only when the probe must exercise a different engine tree or ticket executable. Python remains anchored to the current checkout so every embedded probe runs with the same installed dependencies.
