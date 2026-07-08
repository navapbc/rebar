# Session-provenance capture shims

rebar records *which coding-agent session* claimed a ticket (see
[`docs/config.md`](config.md) "Session provenance" and
[`docs/event-schema.md`](event-schema.md) "Session provenance (`claimed_session`)"). The
record path reads a session id from an ordered env-var list
(`REBAR_SESSION_ID` → `CLAUDE_CODE_SESSION_ID` → `OPENCODE_SESSION_ID` → `SESSION_ID`) and an
optional harness tag from `AI_AGENT`. Some harnesses do not expose a readable session var to a
local CLI, so this page ships copy-paste **shims** that populate the portable `REBAR_SESSION_ID`
(and `AI_AGENT`) from each harness's own lifecycle hook.

> **Two caveats that apply to every shim below.**
>
> - **Id instability (gotcha G1).** A harness session id is NOT stable across
>   `--resume` / `--continue` / fork / subagents — treat the recorded value as "the session
>   that emitted the claim event", not a stable per-ticket lifetime id.
> - **Keep it session-scoped (gotcha G3).** Populate `REBAR_SESSION_ID` only from a
>   session-lifecycle hook (as below) — do NOT `export REBAR_SESSION_ID=…` from a shell
>   profile (`~/.bashrc` etc.), or a single stale value would leak into every unrelated
>   session and mis-attribute their claims.

## Claude Code (local CLI) — SessionStart hook

`CLAUDE_CODE_SESSION_ID` is exposed only in the remote/cloud runner; the local CLI does not
export it. The shim reads the SessionStart hook's `session_id` from stdin and appends exports
to the `$CLAUDE_ENV_FILE` that Claude Code makes available to SessionStart hooks (see the
[Claude Code hooks reference](https://code.claude.com/docs/en/hooks) for the stdin envelope +
`CLAUDE_ENV_FILE` contract). If a future Claude Code changes those names/mechanism, the shim
simply no-ops (no injection file / unparsed id → exit 0) — it never errors or blocks the
session.

Script: [`scripts/session-shims/claude-code-session-start.sh`](../scripts/session-shims/claude-code-session-start.sh).

Wire it up in `.claude/settings.json` (repo-local) or `~/.claude/settings.json` (global):

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "startup|resume|clear|compact",
        "hooks": [
          {
            "type": "command",
            "command": "scripts/session-shims/claude-code-session-start.sh"
          }
        ]
      }
    ]
  }
}
```

The hook exports `REBAR_SESSION_ID=<session_id>` and `AI_AGENT=claude-code`, so a subsequent
local `rebar claim` records
`claimed_session` and `claim_harness`. It is fire-and-forget (always exits 0, never blocks the
session — malformed stdin, a blank `session_id`, an absent `CLAUDE_ENV_FILE`, or an unwritable
env file are silent no-ops) and idempotent: re-firing on resume/clear/compact first strips its
OWN prior exports (preserving other hooks' lines) then re-writes the current session's values,
so the env file never accumulates duplicate/stale lines and always reflects the current
session (whose id may differ from a prior session per the G1 caveat above).

### Verifying the hook is wired

Because the shim fails safe (a wrong/absent `CLAUDE_ENV_FILE` is a silent no-op), confirm it
is actually populating the environment after install:

1. Start a fresh Claude Code session (so `SessionStart` fires).
2. In that session run `echo "$REBAR_SESSION_ID"` — it should print the session id (not empty).
3. Create + claim a throwaway ticket, then `rebar show <id>` — `claimed_session` should equal
   that id and `claim_harness` should be `claude-code`.

If step 2 is empty, the hook is not firing (check the `settings.json` path/matcher) or this
Claude Code version uses a different env-injection mechanism than the
[hooks reference](https://code.claude.com/docs/en/hooks) documents.

## Codex CLI — `shell_environment_policy` (harness tag) + rollout id

Codex differs from Claude Code in two ways that constrain what a shim can do (per the
[Codex config reference](https://developers.openai.com/codex/config-reference) and
[hooks docs](https://developers.openai.com/codex/hooks)):

- A Codex **SessionStart hook CANNOT export env vars** into the shell commands Codex later runs
  (a hook's only structured effect is `additionalContext`) — so there is no Codex equivalent of
  Claude Code's `CLAUDE_ENV_FILE` that could dynamically export `REBAR_SESSION_ID`.
- Codex exposes **no readable session-id env var**; session "rollout" transcripts live under
  `~/.codex/sessions/YYYY/MM/DD/rollout-*-<id>.jsonl` (root overridable via `CODEX_HOME`).

What *is* reliable is `shell_environment_policy.set`, a static map injected into every subprocess
Codex spawns. Copy [`scripts/session-shims/codex-config.toml`](../scripts/session-shims/codex-config.toml)
into `~/.codex/config.toml`:

```toml
[shell_environment_policy]
set = { AI_AGENT = "codex" }
```

This tags every Codex subprocess, so a Codex `rebar claim` records `claim_harness = codex`. To
also record `REBAR_SESSION_ID`, either set a static value in `set` or `export REBAR_SESSION_ID`
from your shell profile with `experimental_use_profile = true` (mind the G3 caveat above); Codex
cannot inject a fresh per-session id automatically.

## Cursor cloud agents — `.cursor/environment.json` install script + Secrets

Cursor's [`environment.json`](https://www.cursor.com/schemas/environment.schema.json) has **no
`env` field**, and Cursor exposes no readable session/agent-id env var (per the
[cloud-agent setup docs](https://cursor.com/docs/cloud-agent/setup)). The first-class way to set
env vars is the dashboard **Secrets** tab (dashboard-managed, environment-scoped — exposed to the
agent as env vars); use it to set `AI_AGENT=cursor` (and `REBAR_SESSION_ID` if you have a value).

As a repo-committed supplement for the harness tag, [`scripts/session-shims/cursor-environment.json`](../scripts/session-shims/cursor-environment.json)
runs [`cursor-provenance.sh`](../scripts/session-shims/cursor-provenance.sh) as its `install`
command:

```json
{ "install": "bash scripts/session-shims/cursor-provenance.sh" }
```

Because a cloud-agent VM is ephemeral and single-session (torn down after the run), the script
appends `export AI_AGENT=cursor` to the VM's shell profile (`~/.bashrc` and `~/.profile`,
idempotently) — the G3 not-in-profile caveat is about long-lived local machines, not throwaway
VMs — so a Cursor `rebar claim` records `claim_harness = cursor`.

**Caveat (best-effort):** a profile append only reaches `rebar` if the agent runs tool commands
through an interactive (`~/.bashrc`) or login (`~/.profile`) shell; a bare non-interactive
non-login `bash -c` would not source either. So the **Secrets tab is the reliable path** — set
`AI_AGENT=cursor` (and `REBAR_SESSION_ID`, which has no native Cursor source) there; treat the
`install`-script append as a supplement for shells that do source a profile.
