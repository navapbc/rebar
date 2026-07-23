# Commit-ticket trailer — every commit references a rebar ticket

rebar can require that **every commit to `main` reference a rebar ticket that resolves in the
ticket store**. It is enforced in CI as part of the Gerrit **Verified** gate (the
`require-ticket` job in `.github/workflows/gerrit-verify.yaml`) and toggled by the
`verify.require_ticket_for_commit` config key. The mechanism is reusable: one
`rebar verify-commit-ticket` command backs the CI gate and can also run locally or from a
client-side hook.

> **Discovering this from the CLI (no repo checkout needed).** The format is served offline by
> **`rebar explain commit-trailer`** (packaged in the wheel), and **`rebar verify-commit-ticket`**
> checks a commit and prints the format on failure — both work in a consuming project that has
> the `rebar` CLI but not this repo. This doc is the deeper reference (CI wiring, config,
> security); the packaged guide is the on-ramp.

## The expected format (what clients must do)

```
Every commit to `main` must reference a rebar ticket. Add a trailer to the commit
message (preferred), or start the subject with the ticket id:

    rebar-ticket: <id>        e.g.  rebar-ticket: blank-guild-koi
  - or a subject prefix -
    <id>: <summary>           e.g.  blank-guild-koi: fix the widget

Accepted <id> forms (resolved against the ticket store):
    alias       blank-guild-koi
    full id     fc9e-8c2e-cb2f-465f
    short id    fc9e-8c2e
    Jira key    REB-310            (project prefix from jira.project)
```

The `rebar-ticket:` trailer is canonical; a leading `<id>:` subject token is accepted as an
alternative. First-element-*only* is deliberately NOT required — a single store lookup costs
the same wherever the id sits, and the trailer keeps the 50-char subject line free.

> The block above is the single source of truth: it is the `EXPECTED_FORMAT` constant in
> `src/rebar/_commands/verify_commit.py`, printed verbatim by the CLI error and `--help`. A
> unit test asserts this doc quotes it, so they cannot drift.

## How resolution works

`rebar verify-commit-ticket` extracts candidate ids from the message and resolves each against
the store via the shared resolver (`resolve_ticket_id`) — the same one `rebar show` / `rebar
resolve` use — so **alias / full id / short id / Jira key** all work identically. Jira keys
resolve through the reconciler's binding store
(`.tickets-tracker/.bridge_state/bindings.json`), so a Jira id resolves only once the ticket
is bound. The store is located by the standard config precedence (`config.tracker_dir()` →
`REBAR_TRACKER_DIR` / `tracker.dir`), overridable per environment.

**Security:** extracted candidates are filtered to a single safe token (no `/`, `..`,
whitespace, control chars) *before* any filesystem lookup, so a crafted
`rebar-ticket: ../../etc/passwd` can never traverse the store. ID-shape matching stays in the
resolver (defined once), so the guard cannot drift from the accepted forms.

## The CLI

```
rebar verify-commit-ticket [--rev <ref> | --message-file <path> | --message <text>]
```

Default reads `--rev HEAD`. Exit codes:

- `0` — a candidate resolved (or a merge commit, or the gate is disabled — a no-op).
- `1` — no candidate resolved; prints the expected-format diagnostic above.
- `2` — an I/O / store-not-mounted error (a bad `--rev`, a missing `--message-file`, or an
  absent store) — kept **distinct** from a missing ticket so CI infra failures are not
  mistaken for a ticketless commit.

## Enabling / disabling

`verify.require_ticket_for_commit` (env `REBAR_VERIFY_REQUIRE_TICKET_FOR_COMMIT`), default
`false`; this repo enables it in `rebar.toml`. Setting it `false` is the rollback — the CI job
then no-ops. See [docs/config.md](config.md).

## CI wiring

The `require-ticket` job checks out the change, mounts the authoritative `tickets` branch from
GitHub (fetch + worktree, retried, with a distinguishable infra-error), sets
`REBAR_TRACKER_DIR`, and runs the command. Its failure aggregates into the run conclusion →
`Verified -1` (fail-fast, before the test matrix). CI does not apply to the `tickets` branch
itself — that never flows through Gerrit.
