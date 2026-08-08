"""Anti-rot gate: the canary keeps policy in Python and delegates delivery to core.

Ticket e602 (shell-to-Python Tier 3) migrated the reconcile-bridge-canary
workflow's classification + alert-lifecycle logic out of YAML run-blocks into
``scripts/canary_bridge.py``. This guard pins that wiring so it cannot rot:

* each of the four subcommands is invoked by exactly one canary step;
* no logic-bearing rebar/gh invocations reappear in YAML run-blocks (the
  allowlisted exceptions are ``rebar init`` plumbing and the private core push
  process boundary);
* the flush step remains a thin, strict, synchronous push-only adapter while
  retry and merge policy stays in ``rebar._store.push``.
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

    ``rebar init`` (store provisioning plumbing) and the thin private core push
    boundary are allowed; other rebar operations go through canary_bridge.py.
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


def test_flush_step_delegates_to_strict_core_push() -> None:
    """The canary keeps its red disposition while core owns CAS retry policy."""
    flush = [run for name, run in _run_blocks().items() if "Flush" in name]
    assert len(flush) == 1, "the flush-unpushed step is missing from the canary"
    body = flush[0]
    invocation = "REBAR_SYNC_PUSH=always python -m rebar._store.push push --tracker . --strict"
    assert invocation in " ".join(body.replace("\\\n", " ").split())
    assert body.count("python -m rebar._store.push") == 1
    for forbidden in ("git push", "git fetch", "git merge", "raw-git-ok"):
        assert forbidden not in body, f"flush step still owns {forbidden!r} instead of core"
