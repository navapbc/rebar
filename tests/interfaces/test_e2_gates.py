"""Tier E E2: clarity-check / check-ac / quality-check / summary — dispatcher parity.

Dual-run parity over varied fixtures exercising the scoring/counting branches of
each heuristic gate (per-type bonuses, AC floor, file-impact section + structured
events, story prose path, blocked-deps summary), in both --output modes.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
from rebar._cli import main
from rebar._engine import dispatcher, engine_env

_TASK_FULL = """## Acceptance Criteria
- [ ] does the thing
- [ ] handles errors
- [ ] tested

## File Impact
- src/rebar/foo.py (modify)
- tests/test_foo.py (new)

This is a sufficiently long description so the >=200 and >=500 char clarity
bonuses can be exercised. It must ensure we verify and expect the right keywords
appear so the quality keyword_count climbs well above one for the gate to pass.
More filler text to comfortably exceed five hundred characters in total length
so the second length bonus also lands deterministically across both impls here.
"""

_TASK_SPARSE = "short"

_STORY = """## Why
We need this.

## What
Build the thing.

## Scope
Just the thing. This should verify and ensure the prose path passes for stories.
"""

_BUG = """## Reproduction Steps
1. do x

Expected: y. Actual: z.

## Acceptance Criteria
- [ ] fixed
"""


def _bash(argv: list[str], repo: Path, stdin: str | None = None) -> tuple[str, str, int]:
    env = engine_env(str(repo))
    env["_TICKET_TEST_NO_SYNC"] = "1"
    cp = subprocess.run(
        ["bash", str(dispatcher()), *argv],
        env=env, cwd=str(repo), capture_output=True, text=True, input=stdin,
    )
    return cp.stdout, cp.stderr, cp.returncode


def _inproc(argv: list[str], capsys, monkeypatch, stdin: str | None = None) -> tuple[str, str, int]:
    capsys.readouterr()
    if stdin is not None:
        import io
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    code = main(argv)
    cap = capsys.readouterr()
    return cap.out, cap.err, code


@pytest.fixture
def store(rebar_repo: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("_TICKET_TEST_NO_SYNC", "1")
    ids = {}
    ids["task_full"] = rebar.create_ticket("task", "Implement foo", description=_TASK_FULL, repo_root=str(rebar_repo))
    ids["task_sparse"] = rebar.create_ticket("task", "x", description=_TASK_SPARSE, repo_root=str(rebar_repo))
    ids["story"] = rebar.create_ticket("story", "A story", description=_STORY, repo_root=str(rebar_repo))
    ids["bug"] = rebar.create_ticket("bug", "A bug", description=_BUG, repo_root=str(rebar_repo))
    # task with structured file-impact events but no File Impact section (supplement path)
    ids["task_fi"] = rebar.create_ticket("task", "FI task", description="A medium body that should verify the supplement path works.\nmust ensure something.", repo_root=str(rebar_repo))
    rebar.set_file_impact(ids["task_fi"], [{"path": "src/x.py", "reason": "r"}], repo_root=str(rebar_repo))
    # blocked ticket for summary
    ids["blocker"] = rebar.create_ticket("task", "Blocker", repo_root=str(rebar_repo))
    ids["blocked"] = rebar.create_ticket("task", "Blocked", repo_root=str(rebar_repo))
    rebar.link(ids["blocker"], ids["blocked"], "blocks", repo_root=str(rebar_repo))
    return rebar_repo, ids


def test_gate_parity(store, capsys, monkeypatch) -> None:
    repo, ids = store
    argvs: list[list[str]] = []
    for key in ("task_full", "task_sparse", "story", "bug", "task_fi", "NOPE"):
        tid = ids.get(key, "NOPE")
        for gate in ("clarity-check", "check-ac", "quality-check"):
            argvs.append([gate, tid])
            if gate != "clarity-check":  # clarity has no --output flag
                argvs.append([gate, tid, "--output", "json"])
    # summary: single, multiple, blocked, unknown; text + json
    argvs.append(["summary", ids["task_full"]])
    argvs.append(["summary", ids["blocked"]])
    argvs.append(["summary", ids["task_full"], ids["blocked"], "NOPE", "--output", "json"])
    argvs.append(["summary", "NOPE"])

    for argv in argvs:
        b_out, b_err, b_code = _bash(argv, repo)
        i_out, i_err, i_code = _inproc(argv, capsys, monkeypatch)
        assert i_out == b_out, f"{argv}: stdout (in-proc {i_out!r} vs bash {b_out!r})"
        assert i_err == b_err, f"{argv}: stderr (in-proc {i_err!r} vs bash {b_err!r})"
        assert i_code == b_code, f"{argv}: exit (in-proc {i_code} vs bash {b_code})"


def test_clarity_stdin_parity(store, capsys, monkeypatch) -> None:
    repo, ids = store
    payload = '{"ticket_type":"task","description":"' + ("x" * 250).replace('"', "") + '\\n## Acceptance Criteria\\n- [ ] a"}'
    b_out, b_err, b_code = _bash(["clarity-check", "--stdin"], repo, stdin=payload)
    i_out, i_err, i_code = _inproc(["clarity-check", "--stdin"], capsys, monkeypatch, stdin=payload)
    assert (i_out, i_err, i_code) == (b_out, b_err, b_code)
