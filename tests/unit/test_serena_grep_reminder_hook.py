"""Contract tests for the `PreToolUse` grep reminder hook (story tired-stonebroke-sable,
epic frail-tsarist-trout).

The hook re-surfaces Serena's symbol tools at the moment an agent reaches for `grep`, because
launch-time guidance in `AGENTS.md` decays: by mid-task, one line of a long document has no
salience against what is in working memory.

These tests EXECUTE the script against the documented `PreToolUse` stdin envelope, because
that is the only thing CI can re-check. The hook itself cannot be observed firing from an
authoring worktree — Claude Code resolves project settings to the main checkout, shared across
worktrees — so a session transcript is corroboration, never the evidence of record.

Two properties matter more than the trigger, and both are safety properties:

* **It can never block.** A reminder that can deny a call would make every false positive an
  outage. So: never a non-zero exit, and never a `permissionDecision` key (which would also
  suppress the permission prompt, changing this repo's security posture).
* **It can never be wrong.** The message names the ONE case where `grep` is genuinely correct,
  so an agent that reads it and continues with `grep` has been helped, not overridden.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from _subprocess_env import subprocess_env

_REPO_ROOT = Path(__file__).resolve().parents[2]
_HOOK = _REPO_ROOT / "scripts" / "hooks" / "serena_grep_reminder.py"
_SETTINGS = _REPO_ROOT / ".claude" / "settings.json"


def _run(payload: object | None, *, raw: str | None = None) -> subprocess.CompletedProcess[str]:
    """Execute the hook exactly as the harness does: JSON on stdin, JSON or nothing on stdout."""
    stdin = raw if raw is not None else json.dumps(payload)
    return subprocess.run(
        [sys.executable, str(_HOOK)],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def _envelope(command: str, tool_name: str = "Bash") -> dict:
    return {
        "session_id": "s1",
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": {"command": command, "description": "d"},
    }


def _context(proc: subprocess.CompletedProcess[str]) -> str | None:
    if not proc.stdout.strip():
        return None
    return json.loads(proc.stdout)["hookSpecificOutput"].get("additionalContext")


# ── happy path ──────────────────────────────────────────────────────────────────────
def test_plain_grep_gets_a_reminder_naming_the_serena_tool():
    """`grep -rn foo src/` — the exact shape from the story — surfaces the reminder."""
    proc = _run(_envelope("grep -rn foo src/"))
    assert proc.returncode == 0, f"hook exited {proc.returncode}: {proc.stderr}"
    context = _context(proc)
    assert context, "no additionalContext emitted for a plain grep"
    assert "find_referencing_symbols" in context, (
        f"the reminder must name the preferred tool; got: {context!r}"
    )


# ── edge / contract ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "command",
    [
        "grep -rn foo src/",
        "rg foo",
        "egrep 'a|b' src",
        "fgrep literal src",
        "git grep -n foo",
        "cat x | grep foo",
        "ls && rg pattern",
    ],
)
def test_every_grep_family_invocation_fires(command: str):
    """The trigger is a plain grep-family match — deliberately dumb, no pattern analysis."""
    assert _context(_run(_envelope(command))), f"no reminder for {command!r}"


@pytest.mark.parametrize(
    "command",
    ["ls -la", "make test", "python -c 'print(1)'", "rebar show 1234", "git status"],
)
def test_non_grep_commands_are_silent(command: str):
    """A reminder on every Bash call would be noise and would get the hook disabled."""
    proc = _run(_envelope(command))
    assert proc.returncode == 0
    assert proc.stdout.strip() == "", f"unexpected output for {command!r}: {proc.stdout!r}"


def test_reminder_names_the_grep_exception_so_it_is_never_misleading():
    """`grep` is genuinely correct for string-named symbols; a reminder that omitted this
    would push an agent away from the only tool that can find the site."""
    context = _context(_run(_envelope("grep -rn foo src/"))) or ""
    assert "monkeypatch.setattr" in context or "getattr" in context, (
        f"the reminder omits the string-literal exception; got: {context!r}"
    )


def test_reminder_is_at_most_three_lines():
    """A standing per-grep cost: it must stay cheap or it will be resented and removed."""
    context = _context(_run(_envelope("grep -rn foo src/")))
    assert context, "no reminder emitted at all (an empty message trivially satisfies a cap)"
    lines = [ln for ln in context.splitlines() if ln.strip()]
    assert 1 <= len(lines) <= 3, f"reminder is {len(lines)} lines, cap is 3:\n{context}"


def test_hook_never_denies_or_suppresses_the_permission_prompt():
    """`permissionDecision` is how a PreToolUse hook decides; omitting it entirely keeps the
    normal permission flow. Emitting even "allow" would suppress the prompt."""
    payload = json.loads(_run(_envelope("grep -rn foo src/")).stdout)
    out = payload["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    assert "permissionDecision" not in out, (
        f"the hook emitted permissionDecision={out.get('permissionDecision')!r} — it must "
        "inform only, never decide"
    )


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "not json at all",
        "{}",
        '{"tool_name": "Bash"}',
        '{"tool_input": {}}',
        "null",
        "[]",
    ],
)
def test_malformed_input_is_a_silent_success(raw: str):
    """Fire-and-forget: a hook that crashes on a shape it did not expect must not turn into a
    failed tool call. Exit 2 in particular BLOCKS the call."""
    proc = _run(None, raw=raw)
    assert proc.returncode == 0, (
        f"hook exited {proc.returncode} on input {raw!r} — a non-zero exit can block the call "
        f"(stderr: {proc.stderr.strip()})"
    )


def test_non_bash_tools_are_ignored():
    """The matcher is Bash-scoped; the script must not depend on the matcher alone."""
    assert _run(_envelope("grep -rn foo", tool_name="Read")).stdout.strip() == ""


def test_reminder_does_not_contradict_the_agents_md_rule():
    """Story D's AC: the shipped reminder and the AGENTS.md table must agree. Both must name
    the LSP's string-literal blind spot as grep's legitimate case."""
    context = _context(_run(_envelope("grep -rn foo src/"))) or ""
    agents = (_REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "monkeypatch.setattr" in agents, "AGENTS.md no longer names the blind spot"
    assert "find_referencing_symbols" in agents
    for token in ("find_referencing_symbols",):
        assert token in context and token in agents, f"{token} missing from one of the two"


# ── wiring ──────────────────────────────────────────────────────────────────────────
def test_settings_wires_the_hook_for_bash_without_losing_the_tool_search_setting():
    """The hook is only real if the harness runs it — and story C's setting must survive."""
    settings = json.loads(_SETTINGS.read_text(encoding="utf-8"))
    assert settings["env"]["ENABLE_TOOL_SEARCH"] == "false", (
        "story C's env.ENABLE_TOOL_SEARCH was dropped while adding the hook"
    )
    entries = settings["hooks"]["PreToolUse"]
    commands = [
        h.get("command", "")
        for entry in entries
        if "Bash" in str(entry.get("matcher", ""))
        for h in entry.get("hooks", [])
    ]
    assert any("serena_grep_reminder.py" in c for c in commands), (
        f"no PreToolUse Bash hook invokes the reminder script; found: {commands}"
    )


def test_hook_script_is_tracked_and_executable_from_the_repo_root():
    """`.claude/` is ignored by default, so the script itself must live in tracked space."""
    proc = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "scripts/hooks/serena_grep_reminder.py"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"hook script is not tracked: {proc.stderr.strip()}"
    assert _HOOK.is_file()


def _all_hook_commands(settings: dict) -> list[str]:
    return [
        h["command"]
        for entries in settings.get("hooks", {}).values()
        for entry in entries
        for h in entry.get("hooks", [])
        if h.get("type") == "command"
    ]


def test_no_hook_command_uses_a_relative_path():
    """Every hook command must be anchored to ``$CLAUDE_PROJECT_DIR`` (or absolute).

    Claude Code executes hook commands via ``/bin/sh`` with whatever cwd the shell last
    had, so a relative command resolves against the tool call's cwd, not the project
    root — it silently fails (exit 127, guidance absent) from any cwd without the file
    (bug c6f6-9724-87fc-43db, observed from ``.tickets-tracker``).
    """
    settings = json.loads(_SETTINGS.read_text(encoding="utf-8"))
    commands = _all_hook_commands(settings)
    assert commands, "no hook commands found in .claude/settings.json"
    for command in commands:
        first_word = command.split()[0].strip("\"'")
        assert first_word.startswith(("$CLAUDE_PROJECT_DIR", "/")), (
            f"hook command {command!r} starts with a RELATIVE path: it resolves against "
            "the shell's cwd at hook time and silently fails from any directory without "
            'it — anchor it as "$CLAUDE_PROJECT_DIR/…"'
        )


def test_hook_command_resolves_from_a_foreign_cwd(tmp_path):
    """The configured command, run exactly as the harness runs it (``/bin/sh -c`` with
    ``$CLAUDE_PROJECT_DIR`` set) from a directory with no ``scripts/``, still emits the
    reminder."""
    settings = json.loads(_SETTINGS.read_text(encoding="utf-8"))
    command = next(c for c in _all_hook_commands(settings) if "serena_grep_reminder.py" in c)
    # Precondition: the foreign cwd really lacks the relative path.
    assert not (tmp_path / "scripts").exists()
    proc = subprocess.run(
        ["/bin/sh", "-c", command],
        input=json.dumps(_envelope("grep -rn foo src/")),
        cwd=tmp_path,
        env=subprocess_env({"CLAUDE_PROJECT_DIR": str(_REPO_ROOT)}),
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert proc.returncode == 0, (
        f"hook command failed from a foreign cwd (exit {proc.returncode}): {proc.stderr.strip()}"
    )
    context = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "find_referencing_symbols" in context
