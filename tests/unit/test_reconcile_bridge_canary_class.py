"""Config-as-artifact guard for the Reconciler Heartbeat Canary's bug-close steps.

The canary (``.github/workflows/reconcile-bridge-canary.yml``) auto-closes its
bot-authored ALERT bug tickets when the monitored condition recovers (the
heartbeat close-on-recovery step and the binding-drift close-on-recovery step).

Ticket ed13 made ``--class <value>`` REQUIRED to close a BUG ticket — even with
``--force-close``. A close command that omits ``--class`` fails at runtime with
"closing a bug ticket requires --class", which turns the canary RED on every
recovery. A CI shell step is not unit-runnable, so this test guards the workflow
*content*: every ``rebar transition ... closed`` command in the canary that
closes a bug alert must carry ``--class``.

Regression guard for bug 0e15 (canary close-on-recovery omitted ``--class``,
reddening main once f436's fix drove binding drift back to zero).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW_PATH = _REPO_ROOT / ".github" / "workflows" / "reconcile-bridge-canary.yml"

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


def _canary_steps() -> list[dict]:
    wf = yaml.safe_load(_WORKFLOW_PATH.read_text())
    return wf["jobs"]["canary"]["steps"]


def _command_text(step: dict) -> str:
    """Run-script of a step with comment lines stripped.

    The canary's close steps carry explanatory comments that mention
    "--class is REQUIRED"; asserting on those would let a mutation that drops the
    real flag slip through. Scan only the command lines.
    """
    return "\n".join(
        line for line in step.get("run", "").splitlines() if not line.lstrip().startswith("#")
    )


def _bug_close_steps() -> list[dict]:
    """Steps whose run-script transitions a bug alert ticket to ``closed``.

    These are exactly the close-on-recovery steps: a ``rebar transition``
    invocation whose target status is ``closed`` and which bypasses the close
    gate with ``--force-close`` (only the bot-alert closes do that).
    """
    steps = []
    for step in _canary_steps():
        run = step.get("run", "")
        if re.search(r"rebar transition\b", run) and "closed" in run and "--force-close" in run:
            steps.append(step)
    return steps


def test_both_close_on_recovery_steps_present() -> None:
    """Sanity: the canary has exactly two bug-close-on-recovery steps."""
    steps = _bug_close_steps()
    assert len(steps) == 2, (
        f"expected 2 bug-close-on-recovery steps, found {len(steps)}: "
        f"{[s.get('name') for s in steps]}"
    )


def test_every_bug_close_carries_class_flag() -> None:
    """Every close-on-recovery step must pass ``--class`` (ticket ed13)."""
    offenders = [s.get("name") for s in _bug_close_steps() if "--class" not in _command_text(s)]
    assert not offenders, (
        f"canary bug-close step(s) omit --class (fails under ed13, reddens main): {offenders}"
    )


def test_bug_close_class_values_are_valid() -> None:
    """Each ``--class`` value must be one accepted by `rebar transition`."""
    for step in _bug_close_steps():
        for value in re.findall(r"--class[= ]+(\S+)", _command_text(step)):
            assert value in _VALID_CLASSES, (
                f"step {step.get('name')!r} uses invalid --class value {value!r}; "
                f"valid: {sorted(_VALID_CLASSES)}"
            )
