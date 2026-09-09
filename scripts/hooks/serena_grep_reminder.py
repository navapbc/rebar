#!/usr/bin/env python3
"""`PreToolUse` hook that reminds Bash grep-family users about Serena.

The hook reads one JSON object from stdin. A Bash command containing a lexical `grep`, `rg`,
`egrep`, or `fgrep` token yields one `PreToolUse` JSON envelope with
`hookSpecificOutput.additionalContext`; the hook never sets `permissionDecision`. Malformed
input, missing fields, non-Bash tools, and nonmatches exit 0 without stdout. Matching
intentionally does not parse commands, patterns, or paths.
"""

from __future__ import annotations

import json
import re
import sys

# Match tokens lexically; do not parse the command, pattern, or path.
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
