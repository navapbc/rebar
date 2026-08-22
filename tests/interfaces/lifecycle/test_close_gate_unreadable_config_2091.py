"""An unreadable config is a FAULT, not a deliberate disable — through the close path.

Bug 2091. Two coupled properties, both observed by driving a REAL close:

1. ``close_plan_review_gate_check`` must report an unreadable config under its own
   verdict, never as ``"disabled"`` (which positively asserts an operator turned the
   gate off). The verdict was previously untested through the close path at all.
2. A skipped gate must install NO in-lock recheck. ``txn.transition_core`` invokes
   ``pre_status_check`` INSIDE the write lock, so installing it on a non-running gate
   both adds a config read under the lock and — if the config becomes readable in the
   window between the pre-lock check and the locked recheck — can BLOCK a close that
   previously succeeded.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import rebar
from rebar import config
from rebar._commands import gates, transition_close

# `[verify` never closes its table header -> tomllib raises -> ConfigError.
_UNREADABLE = "[verify\nrequire_plan_review_for_close = true\n"


def _make(repo: Path) -> str:
    tid = rebar.create_ticket("task", "close under an unreadable config", repo_root=str(repo))
    rebar.claim(tid, assignee="me", repo_root=str(repo))
    return tid


@pytest.fixture
def gate_payloads(monkeypatch) -> list[dict]:
    """Record every payload the REAL close gate returns during a close.

    Length is the observable for "was the in-lock recheck installed and run?" — the
    pre-lock check is call 1, the `pre_status_check` closure is call 2.
    """
    seen: list[dict] = []
    real = gates.close_plan_review_gate_check

    def spy(*args, **kwargs):  # type: ignore[no-untyped-def]
        payload = real(*args, **kwargs)
        seen.append(payload)
        return payload

    monkeypatch.setattr(gates, "close_plan_review_gate_check", spy)
    return seen


def test_unreadable_config_closes_and_is_not_reported_as_disabled(
    rebar_repo: Path, monkeypatch, gate_payloads: list[dict]
) -> None:
    tid = _make(rebar_repo)
    monkeypatch.setattr(transition_close, "_completion_precheck", lambda *a, **k: (None, ""))
    (rebar_repo / "rebar.toml").write_text(_UNREADABLE, encoding="utf-8")
    config.reset_config_cache()

    rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))

    assert rebar.show_ticket(tid, repo_root=str(rebar_repo))["status"] == "closed"
    assert len(gate_payloads) == 1, (
        "an unreadable config installed the in-lock recheck: the gate was consulted "
        f"{len(gate_payloads)} times, so a config read happened INSIDE the write lock"
    )
    assert gate_payloads[0]["ok"] is True, "fail-OPEN posture changed"
    assert gate_payloads[0]["verdict"] == "unreadable", (
        "a config FAULT was reported through the close path as the verdict "
        f"{gate_payloads[0]['verdict']!r} — a deliberate operator disable"
    )
