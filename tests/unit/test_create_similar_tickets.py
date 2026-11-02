"""Pin the ranked similar-ticket advisory at ticket creation.

The normalized-title probe next door catches only byte-identical titles. This one ranks
by text, so a duplicate filed in different words is visible too. It rides every surface
that creates a ticket -- CLI, library, MCP, and ``idea`` -- under the same
``verify.suggest_duplicate_tickets`` setting, and like every create advisory it is
computed after the write lands and can never fail it.

Two further properties are pinned here. Advisory keys reach each surface's return dict
generically, so an advisory added later is not silently dropped from one of them. And
non-graph artifact types are never probed, because a ranked list of similar *tickets*
is meaningless for a session log or an identity and probing would force an index
catch-up on a write that never touches the ticket graph.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Any

import pytest

import rebar
from rebar._commands import composer, recent_creates
from rebar._commands import main as commands_main

pytestmark = pytest.mark.unit

# Two tickets about the same defect, worded differently. Title equality cannot relate
# them; ranked retrieval is the only thing that can.
_FIRST = "Reconcile the Jira bridge worklog projection after a partial sync"
_SECOND = "Jira bridge worklog projection is wrong following a partial sync"


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


# ── the signal: a differently worded near-duplicate is ranked and surfaced ─────


def test_library_create_ranks_a_differently_worded_near_duplicate(
    repo: Path, flag_on: None
) -> None:
    first = rebar.create_ticket("task", _FIRST, return_alias=True)

    second = rebar.create_ticket("task", _SECOND, return_alias=True)

    ids = [r["id"] for r in second["similar_tickets"]]
    assert first["id"] in ids, "the earlier ticket was not retrieved by wording"


def test_a_created_ticket_is_never_its_own_match(repo: Path, flag_on: None) -> None:
    """Catch-up indexes the new ticket before the query runs, so without the exclusion
    every create would rank itself first."""
    rebar.create_ticket("task", _FIRST, return_alias=True)

    second = rebar.create_ticket("task", _SECOND, return_alias=True)

    assert second["id"] not in [r["id"] for r in second["similar_tickets"]]


def test_scores_are_normalized_to_the_documented_interval(repo: Path, flag_on: None) -> None:
    rebar.create_ticket("task", _FIRST, return_alias=True)

    results = rebar.create_ticket("task", _SECOND, return_alias=True)["similar_tickets"]

    assert results, "nothing was retrieved, so the score range is untested"
    assert all(0.0 <= r["score"] <= 1.0 for r in results)


# ── every creating surface carries the advisory ───────────────────────────────


def test_library_idea_carries_the_advisory(repo: Path, flag_on: None) -> None:
    first = rebar.create_ticket("task", _FIRST, return_alias=True)

    res = rebar.idea(_SECOND, return_alias=True)

    assert first["id"] in [r["id"] for r in res["similar_tickets"]]


def test_mcp_create_result_carries_the_advisory(repo: Path, flag_on: None) -> None:
    tools = _collect_mcp_tools()
    first = tools["create_ticket"]("task", _FIRST)

    result = tools["create_ticket"]("task", _SECOND)

    assert first["id"] in [r["id"] for r in result["similar_tickets"]]


def test_cli_create_json_returns_the_array_on_stdout(
    repo: Path, flag_on: None, capsys: pytest.CaptureFixture[str]
) -> None:
    first = rebar.create_ticket("task", _FIRST, return_alias=True)
    capsys.readouterr()

    rc = commands_main(["create", "task", _SECOND, "--output", "json"])
    out = capsys.readouterr()

    assert rc == 0
    created = json.loads(out.out.strip().splitlines()[-1])
    assert first["id"] in [r["id"] for r in created["similar_tickets"]]


def test_cli_create_text_mode_surfaces_the_advisory_on_stderr(
    repo: Path, flag_on: None, capsys: pytest.CaptureFixture[str]
) -> None:
    first = rebar.create_ticket("task", _FIRST, return_alias=True)
    capsys.readouterr()

    rc = commands_main(["create", "task", _SECOND])
    out = capsys.readouterr()

    assert rc == 0
    assert "possibly similar tickets" in out.err
    assert (first["alias"] or first["id"]) in out.err


def test_idea_cli_surfaces_the_advisory_on_stderr(
    repo: Path, flag_on: None, capsys: pytest.CaptureFixture[str]
) -> None:
    rebar.create_ticket("task", _FIRST, return_alias=True)
    capsys.readouterr()

    rc = commands_main(["idea", _SECOND])
    out = capsys.readouterr()

    assert rc == 0
    assert "possibly similar tickets" in out.err, "idea_cli calls create_core directly"


# ── advisory keys reach each return dict without a per-key edit ───────────────


def _emit_synthetic_advisory(monkeypatch: pytest.MonkeyPatch) -> str:
    """Make ``create_core`` return one advisory key that no return site knows about."""
    sentinel = "a brand new advisory nobody hand-copied"
    real = composer.create_core

    def _with_extra(*args: Any, **kwargs: Any) -> dict[str, Any]:
        res = real(*args, **kwargs)
        res["synthetic_advisory"] = sentinel
        return res

    monkeypatch.setattr(composer, "create_core", _with_extra)
    return sentinel


def test_a_new_advisory_reaches_the_create_return_dict(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentinel = _emit_synthetic_advisory(monkeypatch)

    res = rebar.create_ticket("task", _FIRST, return_alias=True)

    assert res["synthetic_advisory"] == sentinel


def test_a_new_advisory_reaches_the_idea_return_dict(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gap this closes: before the generic pass-through, an advisory added to
    ``create`` was silently dropped from ``idea``."""
    sentinel = _emit_synthetic_advisory(monkeypatch)

    res = rebar.idea(_FIRST, return_alias=True)

    assert res["synthetic_advisory"] == sentinel


# ── non-graph artifact types are never probed ─────────────────────────────────


@pytest.mark.parametrize("ticket_type", ["session_log", "code_review", "identity"])
def test_non_graph_artifact_types_are_not_probed(
    repo: Path, flag_on: None, monkeypatch: pytest.MonkeyPatch, ticket_type: str
) -> None:
    """Skipped before retrieval, so no index catch-up is forced on a write that never
    touches the ticket graph."""
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        recent_creates,
        "find_similar",
        lambda *a, **k: calls.append(k) or [],  # type: ignore[func-returns-value]
    )

    results = recent_creates.similar_tickets_advisory(
        str(repo),
        ticket_id="0000-0000-0000-0000",
        ticket_type=ticket_type,
        title=_FIRST,
        description="",
        cfg_root=str(repo),
    )

    assert results == []
    assert calls == [], f"{ticket_type} must not reach retrieval"


def test_a_work_item_type_is_probed(
    repo: Path, flag_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The companion to the skip above: without this, the skip test would also pass if
    nothing were ever probed."""
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        recent_creates,
        "find_similar",
        lambda *a, **k: calls.append(k) or [],  # type: ignore[func-returns-value]
    )

    recent_creates.similar_tickets_advisory(
        str(repo),
        ticket_id="0000-0000-0000-0000",
        ticket_type="task",
        title=_FIRST,
        description="",
        cfg_root=str(repo),
    )

    assert len(calls) == 1
    assert calls[0]["exclude_id"] == "0000-0000-0000-0000"


# ── the gate ──────────────────────────────────────────────────────────────────


def test_the_advisory_is_empty_when_the_setting_is_off(repo: Path) -> None:
    rebar.create_ticket("task", _FIRST, return_alias=True)

    res = rebar.create_ticket("task", _SECOND, return_alias=True)

    assert res["similar_tickets"] == []


# ── a retrieval failure can never disturb a completed write ───────────────────


def test_an_empty_retrieval_leaves_the_create_untouched(
    repo: Path, flag_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(recent_creates, "find_similar", lambda *a, **k: [])

    res = rebar.create_ticket("task", _FIRST, return_alias=True)

    assert res["similar_tickets"] == []
    assert rebar.show_ticket(res["id"], repo_root=str(repo))["title"] == _FIRST


def test_a_raising_retrieval_leaves_the_create_untouched(
    repo: Path, flag_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*_a: Any, **_k: Any) -> list[dict[str, Any]]:
        raise RuntimeError("index exploded")

    monkeypatch.setattr(recent_creates, "find_similar", _boom)

    res = rebar.create_ticket("task", _FIRST, return_alias=True)

    assert res["similar_tickets"] == []
    assert rebar.show_ticket(res["id"], repo_root=str(repo))["title"] == _FIRST


def test_a_raising_retrieval_keeps_the_cli_exit_code_at_zero(
    repo: Path, flag_on: None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _boom(*_a: Any, **_k: Any) -> list[dict[str, Any]]:
        raise RuntimeError("index exploded")

    monkeypatch.setattr(recent_creates, "find_similar", _boom)

    rc = commands_main(["create", "task", _FIRST])

    assert rc == 0
    assert capsys.readouterr().out, "the create confirmation is still printed"


# ── the published contract ────────────────────────────────────────────────────


def test_the_committed_schema_documents_the_advisory(repo: Path, flag_on: None) -> None:
    """A real create result validates against the committed schema, including the
    score's range."""
    from rebar import schemas

    rebar.create_ticket("task", _FIRST, return_alias=True)
    result = rebar.create_ticket("task", _SECOND, return_alias=True)

    schema = schemas.load(schemas.CREATE_RESULT)
    score = schema["properties"]["similar_tickets"]["items"]["properties"]["score"]
    assert (score["minimum"], score["maximum"]) == (0, 1)
    assert result["similar_tickets"], "an empty advisory would not exercise the item schema"
    schemas.validator(schemas.CREATE_RESULT).validate(dict(result))


def test_the_schema_rejects_a_score_outside_the_documented_interval() -> None:
    import jsonschema

    from rebar import schemas

    bad = {"id": "x", "similar_tickets": [{"id": "y", "score": 1.5}]}

    with pytest.raises(jsonschema.ValidationError):
        schemas.validator(schemas.CREATE_RESULT).validate(bad)
