# Jira pressure-test probes (reference / manual)

Live-Jira end-to-end probes kept as **reference tooling** for hardening and
pressure-testing rebar's Jira sync (the reconciler bridge). They were used to
validate bidirectional CRUD across every field and to shake out sync bugs when
making bridge changes.

**These are not part of the automated test suite and are not shipped in the
published wheel.** They live under `scripts/` (outside `src/rebar/`, so they are
excluded from the wheel's `packages = ["src/rebar"]`) and outside `tests/`, so
`pytest` never collects them. Do not wire them into CI — they hit live Jira,
create/edit/delete real issues, and are meant to be run by hand.

## Scripts

- `e2e_validation_probe.sh` — exercises the full bidirectional sync pipeline
  (create → outbound → inbound → idempotency → reconcile-check → cleanup) for a
  single ticket.
- `e2e_field_validation_probe.sh` — systematically tests bidirectional CRUD for
  every field across 10 test tickets (requires `REBAR_FIELD_VALIDATION_PROBE=1`
  to opt in).

## Running

Run manually from the repo root with live Jira credentials in the environment:

```bash
export JIRA_URL=... JIRA_USER=... JIRA_API_TOKEN=...
# optional: JIRA_PROJECT (default in-script), REBAR_ENGINE_DIR, REBAR_TICKET_CLI
bash scripts/jira-pressure-test/e2e_validation_probe.sh

REBAR_FIELD_VALIDATION_PROBE=1 \
  bash scripts/jira-pressure-test/e2e_field_validation_probe.sh
```

By default the probes anchor the rebar engine at `src/rebar/_engine` in the
current repo checkout; override with `REBAR_ENGINE_DIR` / `REBAR_TICKET_CLI` to
point at an installed build instead.
