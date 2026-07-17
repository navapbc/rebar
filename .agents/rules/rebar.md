# rebar — Antigravity agent rules

The canonical, cross-vendor agent & contributor guidance for this repository lives in
**`AGENTS.md`** at the repo root (with the full detail in `docs/` and `CONTRIBUTING.md`).
Google Antigravity's `.agents/rules/*` cannot `@`-import another file, so this is a short
reference-and-summary — read **`AGENTS.md`** for the authoritative protocol.

## Tech stack

- **Core**: Python 3.11+, an event-sourced ticketing system (`rebar`) over a git-backed store.
- **Integrations**: a Jira reconciler (`rebar reconcile`), Gerrit code review, and the Serena
  LSP MCP server for semantic navigation.

## Essentials (see `AGENTS.md` for the full protocol)

- **Setup**: `make install` (editable dev install + the pre-commit gate via `make hooks`);
  activate the repo venv (`source .venv/bin/activate`) before any `rebar`/gate command, and
  confirm `which rebar` resolves to the worktree's `.venv/bin/rebar`.
- **Checks**: `make lint`, `make typecheck`, `make test` (default suite; excludes integration);
  `make check` runs lint + typecheck.
- **Track work in rebar**: search / `rebar ready`, then `rebar claim <id> --assignee <you>`
  *before* editing, and `rebar transition <id> in_progress closed` when the acceptance criteria
  are met; record emergent work with `rebar link <new> <parent> discovered_from`. Gates are on:
  a passing `rebar review-plan` attestation is required to claim, and the completion verifier
  runs on close (green CI is still required before you close).
- **Work in a fresh worktree** branched from current `origin/main`; run every command from
  inside it.
- **Land through Gerrit** (never GitHub PRs): `git push gerrit HEAD:refs/for/main`; every commit
  needs a `rebar-ticket: <id>` trailer and a DCO
  `Signed-off-by: Joe Oakhart <joeoakhart+bot@navapbc.com>` (commit under the bot identity
  `joeoakhart+bot@navapbc.com`); Submit once `LLM-Review = +1` AND `Verified = +1` and all
  comments are resolved.
- **Conventions**: target 200–500 LOC per file (soft cap 800; don't split call-graph seams
  mechanically or create files < 100 LOC); prefer the Serena MCP server over raw grep for
  symbol navigation.

Full detail — the gate protocols, the MCP tool set, the ticket model, and the Gerrit recipe —
is in `AGENTS.md`, `CONTRIBUTING.md`, and `docs/`.
