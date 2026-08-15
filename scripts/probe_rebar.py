#!/usr/bin/env python3
"""Reusable end-to-end PROBE for the rebar ticket system (port of probe-rebar.sh).

Exercises every CLI command and a broad set of edge cases against the REAL rebar
engine, asserting exit codes and output invariants, then prints a PASS/FAIL
summary.

CONTINUE-ON-FAILURE IS THE DESIGN (the shell original deliberately omitted
``set -e``): a failing assertion increments the fail counter and the probe keeps
going, so one regression cannot mask the rest of the surface. The harness exits
non-zero iff any assertion failed. Do not convert assertion helpers to raise.

SAFETY: by default the probe runs in an ISOLATED temporary tracker (its own
REBAR_ROOT), so it never touches this project's real tickets and is safe to run
repeatedly. It still drives the project's installed ``rebar`` (the live engine).
Set PROBE_LIVE=1 to instead exercise the project's real store — in that mode the
probe snapshots the existing ticket set, only removes the tickets it creates,
and verifies the store is unchanged at the end.

Usage:
  python scripts/probe_rebar.py                # isolated tracker (recommended)
  REBAR=/path/to/rebar python scripts/probe_rebar.py
  PROBE_LIVE=1 python scripts/probe_rebar.py   # against the real project store
  PROBE_INJECT_FAIL=1 python scripts/probe_rebar.py  # harness self-test: one
      deliberately failing assertion; the probe must continue past it, report
      it in the summary, and exit non-zero.

Exit: 0 if all checks pass, 1 otherwise.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

# ── rebar command resolution ────────────────────────────────────────────────
_REPO_ROOT = Path(__file__).resolve().parent.parent


def _resolve_rebar() -> str:
    candidate = os.environ.get("REBAR") or (
        str(_REPO_ROOT / ".venv" / "bin" / "rebar")
        if (_REPO_ROOT / ".venv" / "bin" / "rebar").exists()
        else "rebar"
    )
    resolved = shutil.which(candidate)
    if not resolved:
        print(f"FATAL: rebar not found ({candidate})", file=sys.stderr)
        sys.exit(1)
    return str(Path(resolved).resolve())


RB = _resolve_rebar()
if not shutil.which("git"):
    print("FATAL: missing dependency: git", file=sys.stderr)
    sys.exit(1)

PASS = 0
FAIL = 0
OUT = ""  # merged stdout+stderr of the last run() (the shell's 2>&1 capture)
STDOUT = ""  # stdout-only of the last run(), for JSON parsing
RC = 0


def _fail(msg: str) -> None:
    global FAIL
    FAIL += 1
    print(f"  \033[31mFAIL\033[0m: {msg}", file=sys.stderr)


def _pass() -> None:
    global PASS
    PASS += 1


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# ── assertion helpers (continue-on-failure: never raise) ────────────────────
# raw-git-ok: generic command runner, argv supplied by caller
def run(*argv: str) -> None:
    global OUT, STDOUT, RC
    cp = subprocess.run(list(argv), capture_output=True, text=True)
    OUT = (cp.stdout + cp.stderr).rstrip("\n")
    STDOUT = cp.stdout.rstrip("\n")
    RC = cp.returncode


def run_rb(*args: str) -> None:
    run(RB, *args)


def assert_rc(expected: int, label: str) -> None:
    if RC == expected:
        _pass()
    else:
        _fail(f"{label} (exit {RC}, want {expected})\n    out: {OUT[:300]}")


def assert_rc_ne(not_expected: int, label: str) -> None:
    if RC != not_expected:
        _pass()
    else:
        _fail(f"{label} (exit {RC}, want != {not_expected})")


def assert_contains(needle: str, label: str) -> None:
    if needle in OUT:
        _pass()
    else:
        _fail(f"{label} (missing '{needle}' in: {OUT[:300]})")


def assert_not_contains(needle: str, label: str) -> None:
    if needle in OUT:
        _fail(f"{label} (unexpected '{needle}')")
    else:
        _pass()


def assert_eq(expected: object, actual: object, label: str) -> None:
    if expected == actual:
        _pass()
    else:
        _fail(f"{label} (got '{actual}', want '{expected}')")


_ID_RE = re.compile(r"^[0-9a-f]{4}-")
# `create` prints a one-line confirmation embedding the id: `created <alias> (<id>): <title>`.
_CANONICAL_ID_RE = re.compile(r"\b[0-9a-f]{4}(?:-[0-9a-f]{4}){3}\b")


def _last_id() -> str:
    ids = _CANONICAL_ID_RE.findall(OUT)
    return ids[-1] if ids else ""


def _show_json(tid: str) -> dict:
    """Clean `show` for value extraction (the shell's `$RB show X | jq ...`).

    Parse failures return {} so the following assertion FAILS and the probe
    CONTINUES — the jq-pipeline analogue (empty output, not a raised error).
    """
    cp = subprocess.run([RB, "show", tid], capture_output=True, text=True)
    try:
        doc = json.loads(cp.stdout)
    except ValueError:
        return {}
    return doc if isinstance(doc, dict) else {}


def _list_json(*args: str) -> list:
    cp = subprocess.run([RB, "list", *args], capture_output=True, text=True)
    try:
        doc = json.loads(cp.stdout)
    except ValueError:
        return []
    return doc if isinstance(doc, list) else []


# ── environment setup ────────────────────────────────────────────────────────
_CLEAN_DIRS: list[str] = []
_CREATED: list[str] = []


# raw-git-ok: disposable sandbox repo, not the tracker
def _git(*args: str, cwd: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


# raw-git-ok: disposable sandbox repo, not the tracker
def _setup() -> tuple[str, str, str]:
    if os.environ.get("PROBE_LIVE") == "1":
        mode = "LIVE (real project store)"
        root = (
            os.environ.get("REBAR_ROOT")
            or subprocess.run(
                ["git", "-C", str(_REPO_ROOT), "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        )
    else:
        mode = "ISOLATED (temp tracker)"
        tmp = tempfile.mkdtemp()
        _CLEAN_DIRS.append(tmp)
        root = os.path.join(tmp, "repo")
        os.makedirs(root)
        _git("init", "-q", cwd=root)
        _git("config", "user.email", "probe@example.com", cwd=root)
        _git("config", "user.name", "probe", cwd=root)
        _git("commit", "-q", "--allow-empty", "-m", "init", cwd=root)
        # Explicit init: auto-init is TTY-gated by design, so under a
        # non-interactive stdin the first `create` would otherwise fail.
        subprocess.run([RB, "init", "--silent"], cwd=root, check=True)
    root = str(Path(root).resolve())
    os.environ["REBAR_ROOT"] = root
    tracker = os.path.join(root, ".tickets-tracker")
    pre_ids = _ticket_dirs(tracker) if os.environ.get("PROBE_LIVE") == "1" else ""
    os.chdir(root)
    # Skip the network sync (both directions) in isolated/probe runs (no remote).
    os.environ["REBAR_SYNC_PULL"] = "off"
    os.environ["REBAR_SYNC_PUSH"] = "off"
    return mode, tracker, pre_ids


def _ticket_dirs(tracker: str) -> str:
    try:
        names = sorted(n for n in os.listdir(tracker) if _ID_RE.match(n))
    except OSError:
        names = []
    return "\n".join(names)


def mk(*create_args: str) -> str:
    """`create` with OUT/RC propagated for assertions; records the id for cleanup."""
    run_rb("create", *create_args)
    tid = _last_id()
    if tid:
        _CREATED.append(tid)
    return tid


# raw-git-ok: disposable sandbox repo, not the tracker
def _cleanup(tracker: str, pre_ids: str) -> None:
    # Remove only the tickets this probe created; leave pre-existing ones intact.
    if _CREATED and os.path.isdir(tracker):
        subprocess.run(
            ["git", "rm", "-r", "--quiet", *_CREATED],
            cwd=tracker,
            capture_output=True,
        )
        for name in [*_CREATED, ".graph-cache.json"]:
            shutil.rmtree(os.path.join(tracker, name), ignore_errors=True)
        subprocess.run(
            ["git", "commit", "--quiet", "-m", "probe cleanup"],
            cwd=tracker,
            capture_output=True,
        )
    if os.environ.get("PROBE_LIVE") == "1":
        post = _ticket_dirs(tracker)
        if post == pre_ids:
            print("\nLIVE store verified unchanged after cleanup.")
        else:
            print("\n\033[31mWARNING: live store differs after cleanup!\033[0m", file=sys.stderr)
    for d in _CLEAN_DIRS:
        shutil.rmtree(d, ignore_errors=True)


# ════════════════════════════════════════════════════════════════════════════
def _probe() -> None:  # deliberately one linear probe script
    section("create — types, fields, and validation")
    if os.environ.get("PROBE_INJECT_FAIL") == "1":
        # Harness self-test: a deliberately failing assertion. The probe must
        # CONTINUE past this, run every remaining section, and exit non-zero.
        run_rb("--help")
        assert_contains("no-such-needle-for-selftest", "self-test injected failure")
    epic = mk(
        "epic", "PROBE: epic", "--priority", "1", "--assignee", "alice", "--tags", "probe,top"
    )
    assert_rc(0, "create epic")
    story = mk("story", "PROBE: story", "--parent", epic, "--tags", "probe")
    assert_rc(0, "create story --parent")
    task = mk(
        "task",
        "PROBE: task with all fields",
        "--priority",
        "0",
        "--assignee",
        "bob",
        "--description",
        "Body line.\n\n## Acceptance Criteria\n- [ ] a\n- [ ] b",
        "--tags",
        "probe,alpha",
    )
    assert_rc(0, "create task full")
    bug = mk("bug", "PROBE: bug")
    assert_rc(0, "create bug minimal")
    run_rb("create", "widget", "PROBE: bad type")
    assert_rc_ne(0, "create rejects invalid type")
    assert_contains("invalid ticket type", "invalid-type message")
    run_rb("create", "task", "PROBE: bad pri", "--priority", "9")
    assert_rc_ne(0, "create rejects priority>4")

    section("show — alias, short id, missing, fields")
    run_rb("show", task)
    assert_rc(0, "show by id")
    assert_contains('"priority": 0', "show priority field")
    alias = _show_json(task).get("alias", "")
    run_rb("show", alias)
    assert_rc(0, "show by alias")
    run_rb("show", task[:4])
    assert_rc(0, "show by short (4-hex) id")
    run_rb("show", "no-such-ticket-xyz")
    assert_rc_ne(0, "show missing -> non-zero")
    assert_contains('"error": "ticket_not_found"', "show missing JSON envelope")

    section("edit — each field + validation")
    # `--tags` was removed from `edit` (it whole-field-clobbered, racing concurrent
    # tag deltas); the convergent surface is `--set-tags` / `--add-tag` / `--remove-tag`.
    run_rb(
        "edit",
        bug,
        "--title=PROBE: edited",
        "--priority=3",
        "--assignee=carol",
        "--description=d",
        "--set-tags=probe,edited",
    )
    assert_rc(0, "edit multi-field")
    assert_eq(3, _show_json(bug).get("priority"), "edit priority persisted")
    run_rb("edit", bug, "--priority=99")
    assert_rc_ne(0, "edit rejects priority>4")
    run_rb("edit", bug, "--priority=high")
    assert_rc_ne(0, "edit rejects non-numeric priority")
    run_rb("edit", bug, "--ticket_type=widget")
    assert_rc_ne(0, "edit rejects invalid ticket_type")
    run_rb("edit", bug, "--ticket_type=task")
    assert_rc(0, "edit valid ticket_type")

    section("tags — add, idempotent, untag (missing graceful)")
    run_rb("tag", story, "urgent")
    assert_rc(0, "tag add")
    run_rb("tag", story, "urgent")
    assert_rc(0, "tag idempotent")
    tags = _show_json(story).get("tags", [])
    assert_eq(1, len([t for t in tags if t == "urgent"]), "tag appears once")
    run_rb("untag", story, "urgent")
    assert_rc(0, "untag")
    run_rb("untag", story, "nonexistent")
    assert_rc(0, "untag missing graceful")
    assert_eq(1, len(_list_json("--has-tag=alpha")), "list --has-tag filter")

    section("links — relations, cycle, self, invalid, unlink")
    # Exercise each relation on the same pair, clearing between iterations so the
    # inverse blocking relations (blocks/depends_on) don't legitimately form a cycle.
    relations = ("blocks", "depends_on", "relates_to", "duplicates", "supersedes")
    for rel in (*relations, "discovered_from"):
        run_rb("link", task, bug, rel)
        assert_rc(0, f"link {rel}")
        subprocess.run([RB, "unlink", task, bug], capture_output=True)
    run_rb("link", task, bug, "blocks")
    assert_rc(0, "link blocks (set up cycle test)")
    run_rb("link", bug, task, "blocks")
    assert_rc_ne(0, "cycle rejected")
    assert_contains("cycle", "cycle message")
    run_rb("link", task, task, "blocks")
    assert_rc_ne(0, "self-link rejected")
    run_rb("link", task, bug, "frobnicate")
    assert_rc_ne(0, "invalid relation rejected")
    run_rb("link", task, bug)
    assert_rc_ne(0, "link requires a relation")
    run_rb("deps", task)
    assert_rc(0, "deps")
    assert_contains('"ready_to_work"', "deps shape")
    run_rb("unlink", task, bug)
    assert_rc(0, "unlink (pair-scoped)")

    section("claim + optimistic-concurrency (exit 10)")
    run_rb("claim", task, "--assignee", "dave")
    assert_rc(0, "claim open ticket")
    assert_eq("in_progress", _show_json(task).get("status"), "claim -> in_progress")
    run_rb("claim", task, "--assignee", "eve")
    assert_rc(10, "double-claim -> exit 10")
    run_rb("transition", task, "open", "closed")
    assert_rc(10, "stale current_status -> exit 10")

    section("transition — blocked, auto-detect, backward, task close, reopen")
    run_rb("transition", task, "in_progress", "blocked")
    assert_rc(0, "-> blocked")
    run_rb("transition", task, "blocked", "in_progress")
    assert_rc(0, "blocked -> in_progress")
    run_rb("transition", task, "open")
    assert_rc(0, "2-arg auto-detect (in_progress -> open)")
    assert_eq("open", _show_json(task).get("status"), "auto-detect landed on open")
    run_rb("transition", task, "open", "in_progress")
    assert_rc(0, "open -> in_progress")
    run_rb("transition", task, "in_progress", "open")
    assert_rc(0, "in_progress -> open (backward)")
    run_rb("transition", task, "open", "closed")
    assert_rc(0, "task close (no reason needed)")
    run_rb("reopen", task)
    assert_rc(0, "reopen closed -> open")
    assert_eq("open", _show_json(task).get("status"), "reopen status")

    section("close guards — bug --class vocabulary, story/epic verdict-hash")
    rbug = mk("bug", "PROBE: reason-guard bug")
    assert_rc(0, "create fresh bug")
    # Bug close requires a bounded --class <value> (ticket ed13): no class ->
    # reject; invalid class -> reject; a value from the closed vocabulary -> close.
    run_rb("transition", rbug, "open", "closed")
    assert_rc_ne(0, "bug close requires --class")
    run_rb("transition", rbug, "open", "closed", "--class=bogus")
    assert_rc_ne(0, "bug close rejects invalid --class")
    run_rb("transition", rbug, "open", "closed", "--class=regression")
    assert_rc(0, "bug close with valid --class")
    # Verdict-hash close gate is OPT-IN (default off, since 0.2.0): story/epic
    # close succeeds without --verdict-hash. Enforcement when enabled is covered
    # by the GAP-9 test.
    run_rb("transition", story, "open", "closed")
    assert_rc(0, "story close succeeds by default (verdict gate opt-in)")
    # EPIC's only child (STORY) is now closed, so the children guard allows it.
    run_rb("transition", epic, "open", "closed")
    assert_rc(0, "epic close succeeds (child already closed)")

    section("quality gates")
    run_rb("clarity-check", task)
    assert_contains('"score"', "clarity-check JSON")
    run_rb("check-ac", task)
    assert_rc(0, "check-ac pass (AC present)")
    run_rb("check-ac", bug)
    assert_rc_ne(0, "check-ac fail (no AC)")
    run_rb("quality-check", task)
    assert_contains("QUALITY:", "quality-check")
    run_rb("validate", "--output", "json")
    assert_rc_ne(10, "validate repo-wide --output json runs")
    assert_contains('"score"', "validate report")
    run_rb("validate", task)
    assert_rc_ne(0, "validate rejects a ticket id (repo-wide)")

    section("file-impact / verify-commands (+ invalid JSON)")
    run_rb("set-file-impact", task, '[{"path":"a.py","reason":"r"}]')
    assert_rc(0, "set-file-impact")
    run_rb("get-file-impact", task)
    assert_contains('"a.py"', "get-file-impact")
    run_rb("set-file-impact", task, "not-json")
    assert_rc_ne(0, "set-file-impact rejects bad JSON")
    run_rb("set-verify-commands", task, '[{"dd_id":"D1","dd_text":"t","command":"echo"}]')
    assert_rc(0, "set-verify-commands")
    run_rb("get-verify-commands", task)
    assert_contains('"D1"', "get-verify-commands")

    section("scratch set/get/clear")
    run_rb("scratch", "set", task, "k", "v")
    assert_rc(0, "scratch set")
    run_rb("scratch", "get", task, "k")
    assert_contains('"hit"', "scratch get hit")
    run_rb("scratch", "clear", task, "k")
    assert_rc(0, "scratch clear")
    run_rb("scratch", "get", task, "k")
    assert_contains('"miss"', "scratch get miss after clear")

    section("reads — search, ready, next-batch, summary, exists, epics, descendants")
    run_rb("search", "PROBE")
    assert_rc(0, "search")
    assert_contains('"ticket_id"', "search returns states")
    run_rb("ready")
    assert_rc(0, "ready (default id-list)")
    run_rb("ready", "--output", "json")
    assert_rc(0, "ready --output json")
    try:
        ready_type = "array" if isinstance(json.loads(STDOUT), list) else "non-array"
    except ValueError:
        ready_type = "unparseable"
    assert_eq("array", ready_type, "ready --output json is an array")
    run_rb("ready", "--output", "llm")
    assert_rc(0, "ready --output llm")
    run_rb("ready", "--json")
    assert_rc_ne(0, "legacy ready --json rejected (removed)")
    run_rb("summary", task)
    assert_rc(0, "summary")
    assert_not_contains("[unknown]", "summary renders status")
    run_rb("exists", task)
    assert_rc(0, "exists by id")
    run_rb("exists", alias)
    assert_rc(0, "exists by alias")
    run_rb("exists", "no-such-xyz")
    assert_rc_ne(0, "exists absent -> non-zero")
    # Fresh OPEN epic + child for epic-scoped reads (the earlier epic was closed).
    epic2 = mk("epic", "PROBE: open epic")
    mk("task", "PROBE: batch child", "--parent", epic2)
    run_rb("next-batch", epic2)
    assert_rc(0, "next-batch text")
    run_rb("next-batch", epic2, "--output", "json")
    assert_rc(0, "next-batch --output json")
    assert_contains('"batch"', "next-batch JSON shape")
    run_rb("next-batch", epic2, "-o", "json")
    assert_rc(0, "next-batch -o json (short alias)")
    run_rb("list-descendants", epic2)
    assert_rc(0, "list-descendants")
    assert_contains('"stories"', "descendants shape")

    section("--output / -o — canonical structured-output flag (json|llm|text); legacy removed")
    run_rb("show", task, "--output", "llm")
    assert_rc(0, "show --output llm")
    assert_contains('"id"', "show llm short keys")
    run_rb("show", task, "-o", "llm")
    assert_rc(0, "show -o llm (short alias)")
    run_rb("list", "--output", "llm")
    assert_rc(0, "list --output llm")
    run_rb("list", "--output=json")
    assert_rc(0, "list --output=json (equals form)")
    run_rb("show", task, "--format=llm")
    assert_rc_ne(0, "legacy show --format=llm rejected (removed)")
    run_rb("show", task, "-o", "yaml")
    assert_rc_ne(0, "unsupported -o value rejected")
    assert_contains("unsupported output format", "canonical error text")

    section("report --output json — summary/check-ac/quality-check/fsck/bridge fsck")
    run_rb("check-ac", task, "--output", "json")
    assert_contains('"verdict"', "check-ac json verdict")
    assert_contains('"criteria_count"', "check-ac json criteria_count")
    run_rb("quality-check", task, "-o", "json")
    assert_contains('"line_count"', "quality-check json metrics")
    run_rb("summary", task, "--output", "json")
    assert_contains('"blocking_summary"', "summary json shape")
    run_rb("fsck", "--output", "json")
    assert_contains('"issue_count"', "fsck json shape")
    run_rb("bridge", "fsck", "-o", "json")
    assert_contains('"unknown_event_types"', "bridge fsck unknown-event shape")
    assert_contains('"binding_drift"', "bridge fsck binding-drift shape")
    assert_contains('"store_integrity"', "bridge fsck store-integrity shape")

    section("lifecycle --output json — create/claim/transition/reopen/delete result shapes")
    # Parse STDOUT ONLY: `create` prints an advisory warning to STDERR, which the
    # merged OUT capture would fold in and break JSON parsing.
    run_rb("create", "task", "PROBE: lifecycle json", "--output", "json")
    assert_rc(0, "create --output json")
    assert_contains('"id"', "create json has id")
    assert_contains('"alias"', "create json has alias")
    try:
        lcid = json.loads(STDOUT)["id"]
    except (ValueError, KeyError):
        lcid = ""
    if lcid:
        # Not created via mk(): track explicitly so cleanup removes the tombstone.
        _CREATED.append(lcid)
    run_rb("claim", lcid, "--assignee", "probe", "-o", "json")
    assert_rc(0, "claim -o json")
    assert_contains('"status": "in_progress"', "claim json status")
    run_rb("transition", lcid, "in_progress", "closed", "--output", "json")
    assert_rc(0, "transition --output json")
    assert_contains('"newly_unblocked"', "transition json shape")
    run_rb("reopen", lcid, "-o", "json")
    assert_rc(0, "reopen -o json")
    assert_contains('"to": "open"', "reopen json to=open")
    run_rb("delete", lcid, "--user-approved", "--output", "json")
    assert_rc(0, "delete --output json")
    assert_contains('"deleted": true', "delete json shape")

    section("single-reducer parity — show == list == search shape (bug f026)")
    show_keys = sorted(_show_json(task).keys())
    list_keys = sorted(next((x for x in _list_json() if x.get("ticket_id") == task), {}).keys())
    # `list` and `show` are deliberately NOT identical key sets — each carries
    # fields the other omits, BY DESIGN:
    #   - `show` adds the bulky bodies (`comments`, `description`) that lean
    #     `list` drops (opt back in with `list --full`), plus per-ticket
    #     `digest_freshness`, `inbound_deps` (computed inbound edges, bug 05cb),
    #     and `plan_review_health`.
    #   - `list` surfaces `managed_refs`, which `show` omits.
    # Assert the exact symmetric difference in BOTH directions so drift is caught.
    assert_eq(
        ["managed_refs"],
        sorted(set(list_keys) - set(show_keys)),
        "list adds exactly managed_refs over show",
    )
    assert_eq(
        ["comments", "description", "digest_freshness", "inbound_deps", "plan_review_health"],
        sorted(set(show_keys) - set(list_keys)),
        "show adds exactly comments+description+digest_freshness+inbound_deps"
        "+plan_review_health over lean list",
    )
    run_rb("show", task)
    assert_not_contains('"parent_status_uuid"', "internal key not leaked in show")
    task_row: dict[str, Any] = next((x for x in _list_json() if x.get("ticket_id") == task), {})
    assert_eq(
        [{"command": "echo", "dd_id": "D1", "dd_text": "t"}],
        task_row.get("verify_commands"),
        "verify_commands visible in list (single-reducer)",
    )

    section("--help — usage without execution; free-text not intercepted")
    before = len(_list_json())
    run_rb("init", "--help")
    assert_rc(0, "init --help exits 0")
    assert_contains("Usage: rebar init", "init --help shows usage")
    assert_not_contains("initialized", "init --help did not run")
    run_rb("create", "--help")
    assert_rc(0, "create --help")
    assert_contains("Usage: rebar create", "create --help usage")
    assert_eq(before, len(_list_json()), "create --help created nothing")
    run_rb("--help")
    assert_rc(0, "top-level --help")
    assert_contains("Subcommands:", "overview")
    ft = mk("task", "PROBE: title with --help inside")
    assert_rc(0, "free-text --help not intercepted (ticket created)")
    run_rb("show", ft)
    assert_contains("--help", "free-text --help preserved in title")

    section("archive + fsck + compact")
    arch = mk("task", "PROBE: to archive")
    run_rb("archive", arch)
    assert_rc(0, "archive")
    assert_eq("archived", _show_json(arch).get("status"), "archived status")
    run_rb("archive", arch)
    assert_rc(0, "archive idempotent")
    run_rb("compact", task)
    assert_rc(0, "compact")
    run_rb("fsck")
    assert_rc(0, "fsck clean")
    assert_contains("fsck complete", "fsck summary")

    section("delete — friction (requires --user-approved)")
    dele = mk("task", "PROBE: to delete")
    run_rb("delete", dele)
    assert_rc_ne(0, "delete without --user-approved refused")
    run_rb("delete", dele, "--user-approved")
    assert_rc(0, "delete --user-approved")


def main() -> int:
    mode, tracker, pre_ids = _setup()
    print(f"rebar probe — mode: {mode} — rebar: {RB}")
    try:
        _probe()
    finally:
        # Summary first, cleanup after — the shell's trap-EXIT ordering.
        print("\n────────────────────────────────────────")
        print(f"PROBE RESULT: {PASS} passed, {FAIL} failed (mode: {mode})")
        _cleanup(tracker, pre_ids)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
