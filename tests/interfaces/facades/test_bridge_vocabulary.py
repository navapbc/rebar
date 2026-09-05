"""Happy-path contract for the canonical bridge-noun command spellings."""

from __future__ import annotations

import pytest

from rebar._cli import main

pytestmark = pytest.mark.unit


def test_bridge_fsck_routes_the_existing_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    """The canonical noun command forwards arguments to the established audit."""
    captured: list[list[str]] = []

    def fake_main(argv: list[str]) -> int:
        captured.append(argv)
        return 7

    monkeypatch.setattr("rebar._engine_support.bridge_fsck.main", fake_main)
    # Exercise routing without triggering unrelated first-time tracker provisioning;
    # production must retain the legacy alias's auto-init policy.
    monkeypatch.setattr("rebar.config.tracker_dir_override", lambda: "/tmp/bridge-test-tracker")

    assert main(["bridge", "fsck", "--output", "json"]) == 7
    assert captured == [["--output", "json"]]


def test_bridge_check_access_routes_the_existing_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """The canonical noun command forwards to the existing Jira round-trip."""
    captured: list[tuple[list[str], dict[str, str] | None]] = []

    def fake_probe(argv: list[str], *, extra_env: dict[str, str] | None = None) -> int:
        captured.append((argv, extra_env))
        return 6

    monkeypatch.setattr("rebar._cli._bridge_probe", fake_probe)

    assert main(["bridge", "check-access", "--diagnostic"]) == 6
    assert captured == [(["--diagnostic"], None)]


def test_bridge_setup_routes_the_existing_wizard(monkeypatch: pytest.MonkeyPatch) -> None:
    """The canonical noun command is a pass-through into the onboarding parser."""
    captured: list[tuple[list[str], str]] = []

    def fake_onboard(argv: list[str], *, prog: str = "rebar bridge setup") -> int:
        captured.append((argv, prog))
        return 5

    monkeypatch.setattr("rebar._cli._jira_onboard.jira_onboard", fake_onboard)

    assert main(["bridge", "setup", "--url", "https://jira.example"]) == 5
    assert captured == [(["--url", "https://jira.example"], "rebar bridge setup")]
