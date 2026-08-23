"""Create-time advisory same-title duplicate warning (ticket eac3-ed70-764a-4f9e).

Duplicate tickets were only detected after an agent was mid-implementation: the heavy
overlap detector (ADR 0086) runs at review time, so a same-title twin filed seconds after
its original passed creation unflagged — and every same-title true duplicate in the store
was created within 171 seconds of its twin. These tests pin the cheap create-time
complement: with ``verify.suggest_duplicate_tickets`` on, a create whose normalized title
matches another create inside the recency window is SURFACED — with the candidate's id and
status — on each surface's own channel (library dict, ``create``/``idea`` CLI stderr, MCP
result field). And they pin what it must NOT do: never block or fail the create, never fire
for distinct titles or stale entries, never fire (or write the journal) when the flag is
off, and never let a corrupt journal disturb the write.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import rebar
from rebar._commands import main as commands_main
from rebar._commands import recent_creates
from rebar._commands._seam import tracker_dir
from rebar._store.paths import StorePaths

pytestmark = pytest.mark.unit

_TITLE = "Land dependabot PRs #129 + #130 as one Gerrit change"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A git repo with an initialized store, as the CLI/library/MCP surfaces see it."""
    root = tmp_path / "repo"
    root.mkdir()
    for args in (
        ("init", "-q"),
        ("config", "user.email", "test@example.com"),
        ("config", "user.name", "Test"),
    ):
        subprocess.run(["git", *args], cwd=root, check=True)
    monkeypatch.setenv("REBAR_ROOT", str(root))
    rebar.init_repo(repo_root=str(root))
    return root


@pytest.fixture
def flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enable ``verify.suggest_duplicate_tickets``, the key that gates the probe."""
    monkeypatch.setenv("REBAR_VERIFY_SUGGEST_DUPLICATE_TICKETS", "1")


def _journal_path(root: Path) -> Path:
    return Path(StorePaths(str(tracker_dir(str(root)))).sidecar("recent-creates.json"))


def _collect_mcp_tools() -> dict[str, Any]:
    """Register the MCP write tools against a fake server and hand back the callables."""
    from rebar import _mcp_writes

    tools: dict[str, Any] = {}

    class _FakeMCP:
        def tool(self, *_a, **_k):
            def _decorate(fn):
                tools[fn.__name__] = fn
                return fn

            return _decorate

    class _FakeCtx:
        logger = logging.getLogger("test")

        @staticmethod
        def readonly() -> bool:
            return False

        @staticmethod
        def dump(obj):
            return obj

        @staticmethod
        def allow_llm() -> bool:
            return False

    _mcp_writes.register_write_tools(_FakeMCP(), ctx=_FakeCtx())
    return tools


# ── the signal: same title within the window is surfaced, with id and status ──


def test_library_same_title_create_warns_with_candidate_id_and_status(
    repo: Path, flag_on: None
) -> None:
    first = rebar.create_ticket("task", _TITLE, return_alias=True)
    second = rebar.create_ticket("task", _TITLE, return_alias=True)

    warning = second["duplicate_warning"]
    assert warning is not None, "a same-title create inside the window must be surfaced"
    assert (first["alias"] or first["id"]) in warning, "the candidate is not named"
    assert "status open" in warning, "the candidate's status is not stated"
    assert "duplicates" in warning, "the remedy (a duplicates link) is not stated"


def test_first_create_of_a_title_is_silent(repo: Path, flag_on: None) -> None:
    assert rebar.create_ticket("task", _TITLE, return_alias=True)["duplicate_warning"] is None


def test_distinct_titles_are_silent(repo: Path, flag_on: None) -> None:
    rebar.create_ticket("task", "one thing", return_alias=True)
    res = rebar.create_ticket("task", "an entirely different thing", return_alias=True)

    assert res["duplicate_warning"] is None


def test_normalization_folds_case_punctuation_and_whitespace(repo: Path, flag_on: None) -> None:
    rebar.create_ticket("task", "Fix the thing!", return_alias=True)
    res = rebar.create_ticket("task", "  fix   THE thing ", return_alias=True)

    assert res["duplicate_warning"] is not None


def test_an_entry_older_than_the_window_is_silent(repo: Path, flag_on: None) -> None:
    rebar.create_ticket("task", _TITLE, return_alias=True)
    journal = _journal_path(repo)
    payload = json.loads(journal.read_text(encoding="utf-8"))
    for entry in payload["entries"]:
        entry["ts_ns"] -= recent_creates.RECENT_WINDOW_NS + 1
    journal.write_text(json.dumps(payload), encoding="utf-8")

    res = rebar.create_ticket("task", _TITLE, return_alias=True)

    assert res["duplicate_warning"] is None, "a stale entry must have aged out of the probe"


# ── advisory only: the create is never blocked, failed, or altered ─────────────


def test_the_warned_create_still_succeeds(repo: Path, flag_on: None) -> None:
    first = rebar.create_ticket("task", _TITLE, return_alias=True)
    second = rebar.create_ticket("task", _TITLE, return_alias=True)

    assert second["duplicate_warning"] is not None
    for created in (first, second):
        assert rebar.show_ticket(created["id"], repo_root=str(repo))["title"] == _TITLE


def test_a_corrupt_journal_never_disturbs_the_write(repo: Path, flag_on: None) -> None:
    journal = _journal_path(repo)
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text("{not json", encoding="utf-8")

    res = rebar.create_ticket("task", _TITLE, return_alias=True)

    assert res["duplicate_warning"] is None
    assert rebar.show_ticket(res["id"], repo_root=str(repo))["title"] == _TITLE


# ── the gate: flag off means no probe AND no journal footprint ─────────────────


def test_flag_off_is_silent_and_writes_no_journal(repo: Path) -> None:
    rebar.create_ticket("task", _TITLE, return_alias=True)
    res = rebar.create_ticket("task", _TITLE, return_alias=True)

    assert res["duplicate_warning"] is None
    assert not _journal_path(repo).exists(), "flag off must leave zero write-path footprint"


# ── the CLI surfaces: stderr warns, stdout stays pure, exit code stays 0 ───────


def test_cli_create_warns_on_stderr_and_stdout_stays_pure(
    repo: Path, flag_on: None, capsys: pytest.CaptureFixture[str]
) -> None:
    first = rebar.create_ticket("task", _TITLE, return_alias=True)
    capsys.readouterr()

    rc = commands_main(["create", "task", _TITLE, "--output", "json"])
    out = capsys.readouterr()

    assert rc == 0, "the warning must not change the exit code"
    created = json.loads(out.out.strip().splitlines()[-1])
    assert created["id"], "the create must still succeed"
    assert "possible duplicate" in out.err and (first["alias"] or first["id"]) in out.err
    assert "duplicate_warning" not in out.out, "stdout must stay pure json"


def test_idea_cli_warns_on_stderr(
    repo: Path, flag_on: None, capsys: pytest.CaptureFixture[str]
) -> None:
    rebar.create_ticket("task", _TITLE, return_alias=True)
    capsys.readouterr()

    rc = commands_main(["idea", _TITLE])
    out = capsys.readouterr()

    assert rc == 0
    assert "possible duplicate" in out.err, "idea_cli calls create_core directly; it must emit too"


# ── the MCP surface: the warning rides the tool result ─────────────────────────


def test_mcp_create_result_carries_the_warning(repo: Path, flag_on: None) -> None:
    tools = _collect_mcp_tools()
    tools["create_ticket"]("task", _TITLE)
    result = tools["create_ticket"]("task", _TITLE)

    assert result.duplicate_warning is not None
    assert "possible duplicate" in result.duplicate_warning


# ── concurrency: simultaneous same-title creates serialize on the journal ──────


def test_concurrent_same_title_creates_serialize_so_one_observes_the_other(
    repo: Path, flag_on: None
) -> None:
    """Two processes creating the same title at once must not both miss: the journal RMW
    is one flock-held critical section, so whichever runs second sees the first's entry."""
    script = (
        "import rebar, sys\n"
        f"res = rebar.create_ticket('task', {_TITLE!r}, return_alias=True)\n"
        "print('WARNED' if res['duplicate_warning'] else 'SILENT')\n"
    )
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", script],
            cwd=repo,
            stdout=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    outcomes = [p.communicate(timeout=120)[0].strip().splitlines()[-1] for p in procs]

    assert all(p.returncode == 0 for p in procs)
    assert "WARNED" in outcomes, f"neither create observed the other: {outcomes}"
