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
