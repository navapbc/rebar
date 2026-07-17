# rebar — Claude Code entry point

@AGENTS.md

## Claude Code

The canonical agent & contributor guidance for this repo lives in **`AGENTS.md`**, imported
above — the `@`-import loads it into Claude Code's context at launch. This file exists only to
bridge Claude Code to that shared, cross-vendor guidance; it deliberately carries no content
of its own beyond this note.

- Put anything **all** harnesses need in `AGENTS.md`, not here.
- Keep any genuinely Claude-Code-specific instructions in this section, below the import.
- Machine/operator-local settings (this host's Gerrit identities and credential pinning, the
  local `origin/main`-tracking agent) live in the git-ignored `CLAUDE.local.md`.
