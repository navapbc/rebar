"""Oracle suite for scripts/alert_dedup.py — the SHARED bug-filer dedup (bug 63e8).

Bug 63e8 requires the scheduled dependency-advisory lane to reuse the heartbeat canary's
dedup rather than grow a second one. The canary's helpers were not reusable as-is: they
were module-private and the marker was hard-coded, so two lanes sharing them would have
collapsed onto one marker and muted each other's accumulation caps. They were therefore
extracted here with the marker lifted to a parameter.

These tests pin BOTH halves of that claim: the extracted primitives behave as specified,
AND ``canary_bridge`` still routes through them (an extraction that left a copy behind
would be exactly the second implementation the ticket forbids).
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load(name: str) -> ModuleType:
    """Import a repo-root ``scripts/`` module by name.

    tests/scripts/conftest.py puts that directory on sys.path, which is also what makes
    the modules' bare sibling imports (canary_bridge -> alert_dedup) resolve.
    """
    assert (REPO_ROOT / "scripts" / f"{name}.py").is_file()
    return importlib.import_module(name)


@pytest.fixture(scope="module")
def dedup() -> ModuleType:
    return _load("alert_dedup")


class FakeRunner:
    def __init__(self, responses: dict[tuple[str, ...], tuple[int, str, str]] | None = None):
        self.responses = responses or {}
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        self.calls.append(list(argv))
        for prefix, response in self.responses.items():
            if tuple(argv[: len(prefix)]) == prefix:
                return response
        return (0, "", "")


def test_find_alert_ticket_returns_the_open_bug(dedup: ModuleType) -> None:
    runner = FakeRunner({("rebar", "list"): (0, json.dumps([{"ticket_id": "aaaa-1"}]), "")})
    assert dedup.find_alert_ticket(runner, "some-tag") == "aaaa-1"
    assert "--has-tag=some-tag" in runner.calls[0]
    assert "--status=open" in runner.calls[0]


def test_find_alert_ticket_empty_when_none(dedup: ModuleType) -> None:
    runner = FakeRunner({("rebar", "list"): (0, "[]", "")})
    assert dedup.find_alert_ticket(runner, "t") == ""


@pytest.mark.parametrize("response", [(1, "", "boom"), (0, "not json", ""), (0, "", "")])
def test_find_alert_ticket_is_fail_soft(dedup: ModuleType, response: tuple[int, str, str]) -> None:
    runner = FakeRunner({("rebar", "list"): response})
    assert dedup.find_alert_ticket(runner, "t") == ""


def test_recent_marker_comment_is_marker_scoped(dedup: ModuleType) -> None:
    """Two lanes commenting on tickets must never mute each other."""
    now = 1_700_000_000
    payload = json.dumps(
        {"comments": [{"body": "OTHER_LANE_MARKER: hi", "timestamp": (now - 60) * 10**9}]}
    )
    runner = FakeRunner({("rebar", "show"): (0, payload, "")})
    assert dedup.recent_marker_comment(runner, "t", "MY_MARKER:", now) is False
    assert dedup.recent_marker_comment(runner, "t", "OTHER_LANE_MARKER:", now) is True


def test_recent_marker_comment_window(dedup: ModuleType) -> None:
    now = 1_700_000_000
    old = json.dumps({"comments": [{"body": "M: x", "timestamp": (now - 30 * 3600) * 10**9}]})
    runner = FakeRunner({("rebar", "show"): (0, old, "")})
    assert dedup.recent_marker_comment(runner, "t", "M:", now) is False


def test_recent_marker_comment_is_fail_soft(dedup: ModuleType) -> None:
    runner = FakeRunner({("rebar", "show"): (1, "", "boom")})
    assert dedup.recent_marker_comment(runner, "t", "M:", 0) is False


def test_canary_bridge_delegates_rather_than_duplicating() -> None:
    """The extraction must leave no second copy behind."""
    canary = _load("canary_bridge")
    dedup_mod = _load("alert_dedup")
    assert canary._find_alert_ticket is dedup_mod.find_alert_ticket or (
        canary._find_alert_ticket.__module__ == "alert_dedup"
    )
    source = (REPO_ROOT / "scripts" / "canary_bridge.py").read_text(encoding="utf-8")
    assert "alert_dedup" in source
    assert 'rebar", "list"' not in source and '"--has-tag=' not in source


def test_canary_bridge_marker_is_unchanged() -> None:
    canary = _load("canary_bridge")
    assert canary._ALERT_MARKER == "BRIDGE_CANARY_ALERT:"
    assert canary._ACCUMULATION_WINDOW_SECS == 24 * 3600
