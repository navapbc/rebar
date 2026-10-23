"""Test shared alert deduplication and canary adoption.

Each lane has its own marker namespace and accumulation cap. Canary_bridge must use
the shared primitives rather than a second implementation.
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


#: RFC 8259 s6 interoperable integer range; also JS ``Number.MAX_SAFE_INTEGER``.
JS_SAFE_MAX = 2**53 - 1


def test_recent_marker_comment_accepts_the_string_wire_form(dedup: ModuleType) -> None:
    """Accept a decimal-string nanosecond timestamp from JSON output.

    Skipping that wire form would defeat the 24-hour marker cap without raising.
    """
    now = 1_700_000_000
    ns = (now - 60) * 10**9
    assert ns > JS_SAFE_MAX, (
        "the fixture instant is inside the JS-safe range, so it would never be emitted as "
        "a string and this test could not detect the defect"
    )
    payload = json.dumps({"comments": [{"body": "M: hi", "timestamp": str(ns)}]})
    assert f'"timestamp": "{ns}"' in payload, "the fixture must arrive as a JSON string"

    runner = FakeRunner({("rebar", "show"): (0, payload, "")})
    assert dedup.recent_marker_comment(runner, "t", "M:", now) is True, (
        "a string-form timestamp was not recognized, so the 24h accumulation cap "
        "silently stopped working"
    )


def test_a_stale_string_timestamp_still_does_not_suppress(dedup: ModuleType) -> None:
    """LIVENESS: coercion must not turn the window into an unconditional True."""
    now = 1_700_000_000
    ns = (now - 30 * 3600) * 10**9
    assert ns > JS_SAFE_MAX
    payload = json.dumps({"comments": [{"body": "M: x", "timestamp": str(ns)}]})
    runner = FakeRunner({("rebar", "show"): (0, payload, "")})
    assert dedup.recent_marker_comment(runner, "t", "M:", now) is False


def test_a_non_numeric_string_timestamp_is_skipped_not_raised(dedup: ModuleType) -> None:
    """Fail-soft is preserved: junk must not raise out of a best-effort filer."""
    payload = json.dumps({"comments": [{"body": "M: x", "timestamp": "not-a-number"}]})
    runner = FakeRunner({("rebar", "show"): (0, payload, "")})
    assert dedup.recent_marker_comment(runner, "t", "M:", 1_700_000_000) is False


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
