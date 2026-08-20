"""`gate_enabled` must not collapse a FAULT into a POLICY CHOICE (bug f5c4).

An unreadable/malformed config is a fault; a gate deliberately turned off is a policy
choice. Both used to resolve to a bare ``False``, so no caller could tell them apart and
an unreadable config read as "the operator disabled this gate".

These tests pin the DISTINCTION only. They deliberately also pin that the fail-OPEN
posture is UNCHANGED (an unreadable config still lets the operation proceed) — that
posture is a separate, contested policy decision and this bug is not the place it changes.
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


def test_a_caller_does_not_report_an_unreadable_config_as_a_deliberate_disable(
    cfg_root,
) -> None:
    """The distinction must reach a real consumer, not merely exist at the source.

    ``close_plan_review_gate_check`` used to answer ``verdict='disabled'`` for BOTH — a
    positive assertion that an operator turned the gate off, which is false when the
    config simply could not be parsed.
    """
    state = {"ticket_id": "1111-2222-3333-4444", "ticket_type": "story", "status": "in_progress"}

    off = gates.close_plan_review_gate_check(state["ticket_id"], state, repo_root=cfg_root(_OFF))
    unreadable = gates.close_plan_review_gate_check(
        state["ticket_id"], state, repo_root=cfg_root(_UNREADABLE)
    )

    assert off["verdict"] == "disabled"  # unchanged for a readable config
    assert unreadable["verdict"] != "disabled", (
        "an unreadable config is reported as a deliberate disable"
    )
    assert unreadable["verdict"] == "unreadable"
    # Fail-OPEN posture deliberately preserved: the close still proceeds.
    assert off["ok"] is True and unreadable["ok"] is True
