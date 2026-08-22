"""An unreadable config FAILS a close loudly, BEFORE the write lock — through the close path.

Bug 2091 pinned two coupled properties of the then-current fail-OPEN posture: the close
gate reported an unreadable config under its own verdict (never ``"disabled"``), and a
skipped gate installed NO in-lock recheck (``txn.transition_core`` invokes
``pre_status_check`` INSIDE the write lock, so a non-running gate must add no config read
under it).

The 39f8 operator ruling ("Unreadable config should result in an error") retargets the
first property — the close no longer proceeds at all — while the second must SURVIVE the
retarget: the error is raised by the PRE-lock gate check, so an unreadable config still
does zero gate work under the write lock, and no STATUS event is ever written.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import rebar
from rebar import config
from rebar._commands import gates, transition_close
from rebar.config import ConfigError

# `[verify` never closes its table header -> tomllib raises -> ConfigError.
_UNREADABLE = "[verify\nrequire_plan_review_for_close = true\n"


def _make(repo: Path) -> str:
    tid = rebar.create_ticket("task", "close under an unreadable config", repo_root=str(repo))
    rebar.claim(tid, assignee="me", repo_root=str(repo))
    return tid


@pytest.fixture
def core_calls(monkeypatch) -> list[str]:
    """Record whether the locked write (`txn.transition_core`) is ever entered.

    The observable for "the error is raised BEFORE the lock": an unreadable config must
    fail the close during the pre-lock gate check, so the locked write — and with it any
    in-lock `pre_status_check` config read — never runs.
    """
    from rebar._commands import txn

    seen: list[str] = []
    real = txn.transition_core

    def spy(*args, **kwargs):  # type: ignore[no-untyped-def]
        seen.append("transition_core")
        return real(*args, **kwargs)

    monkeypatch.setattr(txn, "transition_core", spy)
    return seen


def test_unreadable_config_fails_the_close_before_the_lock(
    rebar_repo: Path, monkeypatch, core_calls: list[str]
) -> None:
    tid = _make(rebar_repo)
    monkeypatch.setattr(transition_close, "_completion_precheck", lambda *a, **k: None)

    gate_calls: list[dict] = []
    real_check = gates.close_plan_review_gate_check

    def spy(*args, **kwargs):  # type: ignore[no-untyped-def]
        payload = real_check(*args, **kwargs)
        gate_calls.append(payload)
        return payload

    monkeypatch.setattr(gates, "close_plan_review_gate_check", spy)

    (rebar_repo / "rebar.toml").write_text(_UNREADABLE, encoding="utf-8")
    config.reset_config_cache()

    with pytest.raises(ConfigError) as excinfo:
        rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))

    assert "config" in str(excinfo.value), (
        f"the close error does not name the config fault: {str(excinfo.value)!r}"
    )
    assert rebar.show_ticket(tid, repo_root=str(rebar_repo))["status"] == "in_progress", (
        "a close under an unreadable config went through anyway"
    )
    assert core_calls == [], (
        "the unreadable-config error was NOT raised before the lock: the locked write "
        "(txn.transition_core) was entered, so gate work could run inside the lock"
    )
    assert gate_calls == [], (
        "the pre-lock gate check returned a payload instead of raising — a config FAULT "
        f"was resolved to {gate_calls!r}"
    )
