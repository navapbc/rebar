"""Save-time warning for a description the plan-review gate will refuse to admit.

The admission cap (``verify.max_ticket_description_chars``, default 8,000) blocks
``review-plan`` and the completion verifier — but only when one of those runs, which is
long after the description was written. Four sessions in one night discovered the limit
at review time (8,001 / 8,001 / 8,180 chars, and an epic with three children at 11k-22k),
so the cost was paid as rework rather than as a keystroke.

These tests pin the earlier feedback loop: a ``create``/``edit`` that puts the description
over the SAME configured cap warns immediately, on each surface's own channel — CLI stderr,
the library logger, and an MCP result field (the ``push_status`` precedent: an MCP client
reads only the tool result). And they pin what it must NOT do — it never fires when the
plan-review start-work gate is off or the type is exempt, never fires within the cap, and
never blocks, alters, or fails the write it is warning about.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Any

import pytest

import rebar
from rebar._commands import gates
from rebar._commands import main as commands_main

pytestmark = pytest.mark.unit

_CAP = 8_000
_OVER = "y" * (_CAP + 1)
_AT_CAP = "y" * _CAP


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
def gate_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Enable the plan-review START-WORK gate, the condition the warning speaks about."""
    monkeypatch.setenv("REBAR_VERIFY_REQUIRE_PLAN_REVIEW_FOR_CLAIM", "1")


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


# ── AC1: one configured source, shared with the review guard ──────────────────


def test_default_cap_is_8000_and_is_the_guard_s_own_source(repo: Path) -> None:
    """The warning and the review guard read the SAME key, so they cannot disagree."""
    from rebar.config import load_config
    from rebar.llm.plan_review.det_floor import _description_limit

    assert load_config(str(repo)).verify.max_ticket_description_chars == _CAP
    assert _description_limit(str(repo)) == _CAP


def test_configured_cap_moves_the_warning_threshold(
    repo: Path, gate_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A project that raises the cap silences a description the default would flag."""
    monkeypatch.setenv("REBAR_VERIFY_MAX_TICKET_DESCRIPTION_CHARS", str(_CAP + 100))

    assert (
        gates.description_cap_warning(_OVER, "task", ticket_id="t", cfg_root=str(repo)) is None
    ), "the warning ignored the configured cap and used a hardcoded one"

    monkeypatch.setenv("REBAR_VERIFY_MAX_TICKET_DESCRIPTION_CHARS", "100")
    assert gates.description_cap_warning("z" * 101, "task", ticket_id="t", cfg_root=str(repo))


@pytest.mark.parametrize("length", [_CAP - 1, _CAP])
def test_no_warning_within_the_cap(repo: Path, gate_on: None, length: int) -> None:
    """The boundary matches the guard's: 8,000 is admitted, so it must stay silent."""
    assert (
        gates.description_cap_warning("y" * length, "task", ticket_id="t", cfg_root=str(repo))
        is None
    )


# ── AC3: the gate's own applicability decides, and nothing else ───────────────


def test_no_warning_when_the_claim_gate_is_disabled(repo: Path) -> None:
    """Gate off: claim needs no review, so there is nothing to warn about."""
    assert gates.description_cap_warning(_OVER, "task", ticket_id="t", cfg_root=str(repo)) is None


def test_no_warning_for_a_gate_exempt_type(repo: Path, gate_on: None) -> None:
    """A bug is exempt from the start-work gate, so it is never refused admission."""
    assert gates.description_cap_warning(_OVER, "bug", ticket_id="t", cfg_root=str(repo)) is None


def test_warning_states_the_length_the_cap_and_the_consequence(repo: Path, gate_on: None) -> None:
    warning = gates.description_cap_warning(_OVER, "task", ticket_id="t", cfg_root=str(repo))

    assert warning is not None
    assert f"{_CAP + 1:,}" in warning, "the warning does not state the actual length"
    assert f"{_CAP:,}" in warning, "the warning does not state the cap"
    assert "verify.max_ticket_description_chars" in warning, "the key is not named"
    assert "claim" in warning and "review" in warning, "the consequence is not stated"


def test_an_unreadable_cap_never_disturbs_the_write(
    repo: Path, gate_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Advisory means advisory: a broken config degrades to silence, not an exception."""
    monkeypatch.setenv("REBAR_VERIFY_MAX_TICKET_DESCRIPTION_CHARS", "not-a-number")

    assert gates.description_cap_warning(_OVER, "task", ticket_id="t", cfg_root=str(repo)) is None


# ── AC2: the CLI surface warns on stderr, and the write still succeeds ────────


def test_cli_create_warns_on_stderr_and_still_creates(
    repo: Path, gate_on: None, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = commands_main(["create", "task", "oversized", "--description", _OVER, "--output", "json"])
    out = capsys.readouterr()

    assert rc == 0, "the warning must not change the exit code"
    created = json.loads(out.out.strip().splitlines()[-1])
    assert "Warning:" in out.err and "verify.max_ticket_description_chars" in out.err
    assert "description_warning" not in out.out, "stdout must stay pure json"
    stored = rebar.show_ticket(created["id"], repo_root=str(repo))
    assert stored["description"] == _OVER, "the warning altered the description it warned about"


def test_cli_edit_warns_on_stderr(
    repo: Path, gate_on: None, capsys: pytest.CaptureFixture[str]
) -> None:
    ticket = rebar.create_ticket("task", "small", description="ok", repo_root=str(repo))
    capsys.readouterr()

    rc = commands_main(["edit", ticket, f"--description={_OVER}"])
    out = capsys.readouterr()

    assert rc == 0
    assert "Warning:" in out.err and "verify.max_ticket_description_chars" in out.err
    assert rebar.show_ticket(ticket, repo_root=str(repo))["description"] == _OVER


def test_cli_is_silent_when_the_gate_is_off(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert commands_main(["create", "task", "oversized", "--description", _OVER]) == 0

    assert "max_ticket_description_chars" not in capsys.readouterr().err


def test_cli_is_silent_within_the_cap(
    repo: Path, gate_on: None, capsys: pytest.CaptureFixture[str]
) -> None:
    assert commands_main(["create", "task", "at the cap", "--description", _AT_CAP]) == 0

    assert "max_ticket_description_chars" not in capsys.readouterr().err


# ── AC2: the library surface warns on the rebar logger ────────────────────────


def test_library_create_logs_the_warning(
    repo: Path, gate_on: None, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="rebar"):
        ticket = rebar.create_ticket("task", "oversized", description=_OVER, repo_root=str(repo))

    assert any("max_ticket_description_chars" in r.getMessage() for r in caplog.records), (
        "the library surface emitted nothing an embedding caller could observe"
    )
    assert rebar.show_ticket(ticket, repo_root=str(repo))["description"] == _OVER


def test_library_create_carries_the_warning_in_its_result(repo: Path, gate_on: None) -> None:
    created = rebar.create_ticket(
        "task", "oversized", description=_OVER, return_alias=True, repo_root=str(repo)
    )

    assert created["description_warning"], "create_ticket(return_alias=True) dropped it"


def test_library_edit_returns_and_logs_the_warning(
    repo: Path, gate_on: None, caplog: pytest.LogCaptureFixture
) -> None:
    ticket = rebar.create_ticket("task", "small", description="ok", repo_root=str(repo))

    with caplog.at_level(logging.WARNING, logger="rebar"):
        warning = rebar.edit_ticket(ticket, description=_OVER, repo_root=str(repo))

    assert warning and "max_ticket_description_chars" in warning
    assert any("max_ticket_description_chars" in r.getMessage() for r in caplog.records)


def test_library_is_silent_when_the_gate_is_off(
    repo: Path, caplog: pytest.LogCaptureFixture
) -> None:
    ticket = rebar.create_ticket("task", "small", description="ok", repo_root=str(repo))

    with caplog.at_level(logging.WARNING, logger="rebar"):
        assert rebar.edit_ticket(ticket, description=_OVER, repo_root=str(repo)) is None

    assert not [r for r in caplog.records if "max_ticket_description_chars" in r.getMessage()]


def test_library_edit_is_silent_within_the_cap(repo: Path, gate_on: None) -> None:
    ticket = rebar.create_ticket("task", "small", description="ok", repo_root=str(repo))

    assert rebar.edit_ticket(ticket, description=_AT_CAP, repo_root=str(repo)) is None


def test_library_edit_without_a_description_never_warns(repo: Path, gate_on: None) -> None:
    """A title-only edit must not resurrect a warning about an untouched description."""
    ticket = rebar.create_ticket("task", "small", description=_OVER, repo_root=str(repo))

    assert rebar.edit_ticket(ticket, title="renamed", repo_root=str(repo)) is None


# ── AC2: the MCP surface warns in the tool result ─────────────────────────────


def test_mcp_create_returns_the_warning_field(repo: Path, gate_on: None) -> None:
    tools = _collect_mcp_tools()

    out = tools["create_ticket"]("task", "oversized", description=_OVER)

    assert out.description_warning, "an MCP client reads only the result — it was told nothing"
    assert "max_ticket_description_chars" in out.description_warning


def test_mcp_edit_returns_the_warning_field(repo: Path, gate_on: None) -> None:
    tools = _collect_mcp_tools()
    created = tools["create_ticket"]("task", "small", description="ok")

    out = tools["edit_ticket"](created.id, description=_OVER)

    assert out.result == "ok", "the ack text changed; existing consumers break"
    assert out.description_warning and "max_ticket_description_chars" in out.description_warning


def test_mcp_is_silent_when_the_gate_is_off(repo: Path) -> None:
    tools = _collect_mcp_tools()

    created = tools["create_ticket"]("task", "oversized", description=_OVER)
    edited = tools["edit_ticket"](created.id, description=_OVER + "z")

    assert created.description_warning is None
    assert edited.description_warning is None


def test_mcp_is_silent_within_the_cap(repo: Path, gate_on: None) -> None:
    tools = _collect_mcp_tools()

    assert tools["create_ticket"]("task", "at the cap", description=_AT_CAP)
