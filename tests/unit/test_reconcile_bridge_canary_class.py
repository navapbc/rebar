"""Config-as-artifact guard for the canary's bug-close commands (ticket ed13).

Ticket ed13 made ``--class <value>`` REQUIRED to close a BUG ticket — even with
``--force-close``. A close command that omits ``--class`` fails at runtime with
"closing a bug ticket requires --class", which turns the canary RED on every
recovery (regression guard for bug 0e15).

Ticket e602 migrated the canary's close-on-recovery commands out of workflow
YAML into ``scripts/canary_bridge.py``, so this guard re-points there: it
drives both alert-lifecycle close paths through ``main()`` with a fake runner
and asserts every ``rebar transition`` argv carries a valid ``--class``. A
YAML-side assertion keeps the old scan from passing vacuously: no ``rebar
transition`` may reappear in the canary workflow's run-blocks.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest
import yaml

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW_PATH = _REPO_ROOT / ".github" / "workflows" / "reconcile-bridge-canary.yml"
_SCRIPT = _REPO_ROOT / "scripts" / "canary_bridge.py"

# Valid bug-close classes per `rebar transition --help` (ticket ed13).
_VALID_CLASSES = {
    "regression",
    "plan_defect",
    "env_integration",
    "flaky",
    "preexisting",
    "not_a_bug",
    "duplicate",
    "escalated",
    "undetermined",
}


@pytest.fixture(scope="module")
def mod() -> ModuleType:
    spec = importlib.util.spec_from_file_location("canary_bridge_class_guard", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        self.calls.append(list(argv))
        if argv[:2] == ["rebar", "list"]:
            return (0, json.dumps([{"ticket_id": "alert-ticket-1"}]), "")
        return (0, "", "")

    def transitions(self) -> list[list[str]]:
        return [c for c in self.calls if c[:2] == ["rebar", "transition"]]


def _close_transitions_for(
    mod: ModuleType, subcommand: str, env: dict[str, str]
) -> list[list[str]]:
    runner = _RecordingRunner()
    rc = mod.main([subcommand], runner=runner, environ=env, now_epoch=1_785_800_000)
    assert rc == 0, f"{subcommand} close path exited {rc}"
    transitions = runner.transitions()
    assert transitions, f"{subcommand} recovery made no rebar transition call"
    return transitions


def _heartbeat_recovery_env() -> dict[str, str]:
    return {
        "DRY_RUN": "false",
        "ALERT_TAG": "heartbeat-alert",
        "ALERT_WINDOW_HOURS": "2",
        "STALE": "false",
        "LAST_RUN_AGO": "0h 9m ago",
        "STATUS_MSG": "Reconciler healthy — last successful run was 0h 9m ago.",
        "RUN_URL": "https://example.invalid/run/1",
    }


def _drift_recovery_env() -> dict[str, str]:
    return {
        "DRY_RUN": "false",
        "DRIFT_FOUND": "false",
        "DRIFT_TOTAL": "0",
        "DRIFT_SUMMARY": "none",
        "RUN_URL": "https://example.invalid/run/1",
    }


@pytest.mark.parametrize(
    ("subcommand", "env_factory"),
    [
        ("heartbeat-alert", _heartbeat_recovery_env),
        ("binding-drift-alert", _drift_recovery_env),
    ],
)
def test_every_bug_close_carries_valid_class(mod: ModuleType, subcommand: str, env_factory) -> None:
    """Both close-on-recovery paths must pass a valid ``--class`` (ed13/0e15)."""
    for argv in _close_transitions_for(mod, subcommand, env_factory()):
        assert "--class" in argv, (
            f"{subcommand} close omits --class (fails under ed13, reddens the "
            f"canary on every recovery): {argv}"
        )
        value = argv[argv.index("--class") + 1]
        assert value in _VALID_CLASSES, (
            f"{subcommand} uses invalid --class value {value!r}; valid: {sorted(_VALID_CLASSES)}"
        )
        assert any(a.startswith("--force-close") for a in argv), (
            f"{subcommand} close must bypass the completion-verification gate "
            f"(bot alert tickets have no verifiable criteria — bug 0dc5): {argv}"
        )


def test_no_rebar_transition_left_in_canary_yaml() -> None:
    """The YAML must stay thin: close commands live in canary_bridge.py only."""
    wf = yaml.safe_load(_WORKFLOW_PATH.read_text(encoding="utf-8"))
    offenders = [
        step.get("name")
        for step in wf["jobs"]["canary"]["steps"]
        if isinstance(step, dict) and "rebar transition" in step.get("run", "")
    ]
    assert not offenders, (
        f"rebar transition crept back into canary YAML run-blocks (the ed13 "
        f"--class guard only covers scripts/canary_bridge.py): {offenders}"
    )
