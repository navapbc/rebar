"""Anti-rot gate: the canary's logic must stay in scripts/canary_bridge.py.

Ticket e602 (shell-to-Python Tier 3) migrated the reconcile-bridge-canary
workflow's classification + alert-lifecycle logic out of YAML run-blocks into
``scripts/canary_bridge.py``. This guard pins that wiring so it cannot rot:

* each of the four subcommands is invoked by exactly one canary step;
* no logic-bearing rebar/gh invocations reappear in YAML run-blocks (the
  allowlisted exceptions: ``rebar init`` plumbing and the verbatim-tested CAS
  flush loop, which ticket 4c4f keeps in YAML deliberately);
* the flush loop's CAS retry shape is still present (guard against an
  accidental migration of the loop into the module).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CANARY = _REPO_ROOT / ".github" / "workflows" / "reconcile-bridge-canary.yml"

_SUBCOMMANDS = (
    "check-heartbeat",
    "heartbeat-alert",
    "check-binding-drift",
    "binding-drift-alert",
)


def _steps() -> list[dict]:
    doc = yaml.safe_load(_CANARY.read_text(encoding="utf-8"))
    return [s for s in doc["jobs"]["canary"]["steps"] if isinstance(s, dict)]


def _run_blocks() -> dict[str, str]:
    return {s.get("name", "?"): s["run"] for s in _steps() if "run" in s}


@pytest.mark.parametrize("subcommand", _SUBCOMMANDS)
def test_each_subcommand_invoked_exactly_once(subcommand: str) -> None:
    hits = [
        name
        for name, run in _run_blocks().items()
        if f"scripts/canary_bridge.py {subcommand}" in run
    ]
    assert len(hits) == 1, (
        f"canary must invoke `scripts/canary_bridge.py {subcommand}` in exactly one "
        f"step; found in: {hits}"
    )


def test_module_exists() -> None:
    assert (_REPO_ROOT / "scripts" / "canary_bridge.py").is_file(), (
        "scripts/canary_bridge.py is missing — the canary workflow invokes it"
    )


def test_no_logic_bearing_rebar_calls_left_in_yaml() -> None:
    """Direct rebar ticket/fsck operations must not reappear in run-blocks.

    ``rebar init`` (store provisioning plumbing) is the single allowed direct
    call; every other rebar operation goes through scripts/canary_bridge.py.
    """
    offenders: list[str] = []
    for name, run in _run_blocks().items():
        if "canary_bridge.py" in run:
            continue
        needles = (
            "rebar list",
            "rebar create",
            "rebar comment",
            "rebar transition",
            "rebar bridge-fsck",
        )
        for needle in needles:
            if needle in run:
                offenders.append(f"{name}: {needle}")
    assert not offenders, (
        f"logic-bearing rebar calls have crept back into canary YAML (migrate them "
        f"into scripts/canary_bridge.py — ticket e602): {offenders}"
    )


def test_flush_cas_loop_stays_in_yaml_verbatim_shape() -> None:
    """The CAS push-retry loop stays in YAML (4c4f decision) — never migrate it."""
    flush = [run for name, run in _run_blocks().items() if "Flush" in name]
    assert len(flush) == 1, "the flush-unpushed step is missing from the canary"
    body = flush[0]
    for marker in (
        "for attempt in 1 2 3 4 5",
        "git push origin HEAD:tickets",
        "git merge --no-edit origin/tickets",
        "+tickets:refs/remotes/origin/tickets",
    ):
        assert marker in body, (
            f"flush loop lost its CAS marker {marker!r} — the verbatim-tested YAML "
            f"loop (ticket 4c4f) must not be altered or migrated"
        )
