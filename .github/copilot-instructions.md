# rebar — GitHub Copilot entry point

The canonical agent & contributor guidance for this repo lives in **`AGENTS.md`** at the
repository root. GitHub Copilot's coding agent already reads `AGENTS.md` natively, exactly as
Codex does (see `AGENTS.md`'s own note on this); this file exists so that other Copilot
surfaces — Copilot Chat and the Copilot CLI running in this checkout — are pointed at that same
canonical source instead of drifting from it, mirroring the bridge `CLAUDE.md` provides for
Claude Code via its `@AGENTS.md` import.

**Read and follow `AGENTS.md` in full before doing any work in this repository.** It covers,
among other things:

- Recording work in rebar tickets (not scratch notes), and claiming a ticket before touching
  code.
- The parallel-agent ticket workflow (`list`/`search` → `ready`/`next-batch` → `claim` → work →
  `transition closed`).
- The module-size policy and code-navigation tooling (Serena vs. `grep`) for editing rebar
  itself.
- The Gerrit-based git workflow for landing changes — `main` is **not** merged via GitHub PRs;
  changes go through `git push gerrit HEAD:refs/for/main` and require `LLM-Review +1` and
  `Verified +1` before Submit.
- Pointers into `docs/` for ticket model, gate protocols, MCP tools, concurrency, and more.

- Put anything **all** harnesses need in `AGENTS.md`, not here.
- Keep any genuinely Copilot-specific instructions in this file, below this note.
- Do not duplicate `AGENTS.md` content here beyond this summary — if the two ever diverge,
  `AGENTS.md` is authoritative.
