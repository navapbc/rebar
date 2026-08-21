"""`gate_enabled` must not collapse a FAULT into a POLICY CHOICE (bug f5c4).

An unreadable/malformed config is a fault; a gate deliberately turned off is a policy
choice. Both used to resolve to a bare ``False``, so no caller could tell them apart and
an unreadable config read as "the operator disabled this gate".

These tests pin the DISTINCTION. They deliberately also pin that the fail-OPEN posture
is UNCHANGED (an unreadable config still lets the operation proceed) — that posture is a
separate, contested policy decision and neither bug is the place it changes.

The close-gate payload guard at the end of this module was RE-ANCHORED by bug 2091, which
discharged f5c4's deliberate deferral and split the verdict; see its docstring.
"""

from __future__ import annotations

import pathlib

import pytest

from rebar import config
from rebar._commands import gates

_ATTR = "require_plan_review_for_close"

# A readable config with the gate explicitly on / off, and a malformed one whose
# `[verify` table header never closes -> tomllib raises -> ConfigError.
_ON = f"[verify]\n{_ATTR} = true\n"
_OFF = f"[verify]\n{_ATTR} = false\n"
_UNREADABLE = f"[verify\n{_ATTR} = true\n"


@pytest.fixture
def cfg_root(tmp_path):
    """A repo root whose ``rebar.toml`` this test rewrites between probes.

    Yields a writer; each call rewrites the config and drops the parse cache (which is
    keyed on mtime+size, so an in-test rewrite could otherwise be served stale).
    """
    root = tmp_path / "repo"
    root.mkdir()

    def write(text: str) -> str:
        pathlib.Path(root, "rebar.toml").write_text(text)
        config.reset_config_cache()
        return str(root)

    yield write
    config.reset_config_cache()


def _resolve(root: str):
    return gates.gate_enabled(
        root, _ATTR, ticket_id="1111-2222-3333-4444", gate_label="the plan-review close gate"
    )


def test_gate_off_and_config_unreadable_are_distinguishable(cfg_root) -> None:
    """THE bug: a fault and a policy choice must not resolve to the same value."""
    off = _resolve(cfg_root(_OFF))
    unreadable = _resolve(cfg_root(_UNREADABLE))

    assert off != unreadable, (
        "a deliberately disabled gate and an UNREADABLE config resolved to the same "
        f"value ({off!r}); a fault is being laundered into a policy choice"
    )


def test_each_of_the_three_states_resolves_to_its_own_named_value(cfg_root) -> None:
    """All three states are total and separately nameable by a caller."""
    resolved = {
        "on": _resolve(cfg_root(_ON)),
        "off": _resolve(cfg_root(_OFF)),
        "unreadable": _resolve(cfg_root(_UNREADABLE)),
    }

    assert len(set(resolved.values())) == 3, f"states collapsed: {resolved}"
    assert resolved["on"] is gates.GateState.ENABLED
    assert resolved["off"] is gates.GateState.DISABLED
    assert resolved["unreadable"] is gates.GateState.UNREADABLE


def test_truthiness_is_unchanged_so_the_fail_open_posture_is_preserved(cfg_root) -> None:
    """Bool-compatibility is load-bearing: every existing caller and the ~15 test modules
    that monkeypatch this symbol with a plain bool must keep working, and an unreadable
    config must keep FAILING OPEN (skip the gate) exactly as before."""
    assert bool(_resolve(cfg_root(_ON))) is True
    assert bool(_resolve(cfg_root(_OFF))) is False
    assert bool(_resolve(cfg_root(_UNREADABLE))) is False


def test_the_close_gate_reports_a_config_fault_distinctly_from_a_deliberate_disable(
    cfg_root,
) -> None:
    """RE-ANCHORED by bug 2091 — this test previously pinned the OPPOSITE.

    It was written here as a deliberate DEFERRAL guard, asserting the close gate's payload
    was IDENTICAL for both states while the source distinguished them, so that f5c4 landed
    as a genuinely zero-behaviour-change source fix. The reason it could not be split then
    was `transition_close`'s `verdict != "disabled"` proxy for "the gate ran": a second
    non-running verdict would have silently installed the in-lock recheck.

    Bug 2091 removed that proxy (the payload now carries an explicit `gate_ran` stamp), so
    the deferral is discharged and this guard is re-pointed at the behaviour it was always
    guarding the way TO. Its intent is unchanged: the close gate must not launder a config
    FAULT into an operator's POLICY CHOICE.
    """
    state = {"ticket_id": "1111-2222-3333-4444", "ticket_type": "story", "status": "in_progress"}

    off = gates.close_plan_review_gate_check(state["ticket_id"], state, repo_root=cfg_root(_OFF))
    unreadable = gates.close_plan_review_gate_check(
        state["ticket_id"], state, repo_root=cfg_root(_UNREADABLE)
    )

    assert off["verdict"] == "disabled"
    assert unreadable["verdict"] == "unreadable", (
        "a config FAULT is still reported as a deliberate disable"
    )
    assert off != unreadable

    # The fail-OPEN posture is UNCHANGED — still a contested policy this bug does not move.
    assert off["ok"] is True and unreadable["ok"] is True

    # Neither skip verdict ran the gate, so neither may install in-lock work.
    assert off["gate_ran"] is False and unreadable["gate_ran"] is False

    # ...and the SOURCE still tells them apart, which is what f5c4 was about.
    assert _resolve(cfg_root(_OFF)) is not _resolve(cfg_root(_UNREADABLE))
