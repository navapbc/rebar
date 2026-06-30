# ADR 0007: How `rebar jira-onboard` persists Jira config (stdlib-only, never edits pyproject)

- **Status:** Accepted
- **Context:** Story *Add interactive `rebar jira-onboard` wizard* (`b5db-7433`). The
  config READ path (`load_config().jira.*`, `resolve_jira_settings`) and `bridge-probe`
  already existed; this ADR records the WRITE-path decisions the wizard introduced
  (`rebar.config.write_jira_config`).

## Context

The wizard must persist the three non-secret Jira coordinates (`url` / `user` /
`project`) to the typed config that the reconciler already reads. The config can be a
standalone `rebar.toml`, a `pyproject.toml` `[tool.rebar.jira]` table, or a legacy
`.rebar/config.conf`. Two forces collided:

- Python's stdlib `tomllib` is **read-only** — there is no stdlib TOML *writer*. A
  general-purpose surgical TOML editor (preserving comments, inline tables, dotted
  keys, multiline strings in an arbitrary user-managed file) is a well-known
  correctness hazard. The standard library for this is `tomlkit`.
- rebar's engine is **deliberately stdlib-only** (`dependencies = []`; the core wheel
  pulls only `pyyaml`/`jsonschema`/`referencing`). Adding `tomlkit`/`tomli-w` for one
  onboarding writer would regress that constraint.

## Decision

1. **Never edit a user-owned `pyproject.toml`.** The write target is always a
   rebar-owned `rebar.toml` (or, conceptually, the legacy conf — but we persist forward
   to `rebar.toml`). When the discovered project config is a `pyproject.toml` (or none
   exists), the wizard **creates** `rebar.toml` at the repo root. `rebar.toml` is probed
   *before* `pyproject.toml` by `_discover_project_config`, so the fresh file wins read
   precedence. The wizard prints that a `rebar.toml` was created and that deleting it
   reverts to the prior pyproject-based config.

2. **No TOML-writer dependency; no surgical text-splicing.** `write_jira_config` reads
   the (rebar-owned) target whole with stdlib `tomllib` into a dict — so `[jira]`,
   `jira = {…}` inline-table, and `jira.url` dotted-key forms all normalize to the same
   nested dict (no form-specific code, no possibility of appending a duplicate
   section) — mutates the `jira` table in memory, and re-emits the **entire file** via a
   small, self-contained `_emit_toml` (only the scalar/list types rebar config uses).
   Because the whole file is re-serialized, there is **no section-end-boundary problem**
   to get wrong. Comment loss on a rebar-owned config file is acceptable (we never
   re-emit a user pyproject).

3. **Atomic, fail-closed.** The write goes to a temp file in the same directory then
   `os.replace`, so no torn/partial file is possible. A malformed existing `rebar.toml`
   raises `ConfigError` and nothing is written. The read-modify-write is last-writer-
   wins across concurrent writers — acceptable for an interactive single-operator tool.

4. **The secret stays env-only.** `JIRA_API_TOKEN` is never a config key and is never
   written; the wizard only guides the operator to keep it in the environment.

## Consequences

- The write path stays consistent with the read path and adds zero dependencies.
- A future contributor must NOT add `pyproject.toml` write support to rebar tooling, and
  must NOT add a TOML-writer dependency to the core, without revisiting this ADR.
- If round-trip fidelity (comment preservation) on a shared config file ever becomes a
  real requirement, that is the trigger to reconsider `tomlkit` — but only for that case.
