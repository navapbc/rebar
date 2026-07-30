#!/usr/bin/env python3
"""`PreToolUse` hook: remind an agent about Serena's symbol tools when a `Bash` command
invokes the grep family.

Why this exists: `AGENTS.md` §"Navigating the codebase" already tells agents to prefer
Serena's `find_referencing_symbols` / `find_symbol` over `grep` for finding call sites — but
launch-time guidance decays over a long session, and an agent with Serena configured has still
been observed hand-enumerating references with `grep` for many turns. A reminder delivered by
the harness at the moment of the action does not decay the way a line read once at launch does.

Why the trigger is deliberately dumb: this hook matches `grep`/`rg`/`egrep`/`fgrep` appearing
anywhere in the command string — full stop. It does NOT try to classify whether the searched
pattern "looks like a symbol", whether the path is under `src/`, or otherwise parse the
command. A false positive (nudging on a grep that was already the right call) costs three
lines of ignorable text. A false negative (staying silent on a grep that should have been a
Serena call) costs the exact behaviour this hook exists to fix. And a clever predicate that
occasionally fires on the wrong thing is worse than a dumb one that always fires on the right
family of commands: a hook an agent learns to distrust gets ignored or disabled, so simple and
consistent beats clever and occasionally wrong.

Contract (see tests/unit/test_serena_grep_reminder_hook.py):
  * Reads a single JSON object from stdin (the standard `PreToolUse` envelope).
  * Only acts when `tool_name == "Bash"` and `tool_input.command` contains a grep-family token.
  * On a match, writes a `PreToolUse` JSON envelope with `hookSpecificOutput.additionalContext`
    to stdout and exits 0. `permissionDecision` is deliberately never set, so the normal
    permission flow still applies -- this hook only ever adds context, never a decision.
  * On anything else -- no match, malformed/empty stdin, missing fields, non-Bash tool -- it
    exits 0 with no stdout. It must never raise, never block (exit 2 blocks the tool call),
    and never print anything but the one JSON envelope.
"""

from __future__ import annotations

import json
import re
import sys

# Deliberately dumb: any of these tokens anywhere in the command string is a match. No
# attempt to parse the command, classify the pattern, or check the path.
_GREP_FAMILY = re.compile(r"(?<![\w-])(grep|rg|egrep|fgrep)(?![\w-])")

_REMINDER = (
    "Prefer Serena's find_referencing_symbols / find_symbol for call sites and references -- "
    "it is semantic and skips comment/string false positives. grep is still the right tool for "
    "a symbol named as a string (monkeypatch.setattr, getattr, importlib) and for current line "
    "numbers (Serena's numbering is offset and its index can lag edits)."
)


def _extract_command(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    if payload.get("tool_name") != "Bash":
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    command = tool_input.get("command")
    if not isinstance(command, str) or not command.strip():
        return None
    return command


def main() -> int:
    try:
        raw = sys.stdin.read()
    except (OSError, UnicodeDecodeError, ValueError):
        return 0
    if not raw or not raw.strip():
        return 0
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return 0

    command = _extract_command(payload)
    if command is None:
        return 0
    if not _GREP_FAMILY.search(command):
        return 0

    output = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": _REMINDER,
        }
    }
    try:
        sys.stdout.write(json.dumps(output))
    except (OSError, ValueError):
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
