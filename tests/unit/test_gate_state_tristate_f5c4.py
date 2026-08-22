"""`gate_enabled` must not collapse a FAULT into a POLICY CHOICE (bug f5c4 -> ruling 39f8).

History in two steps. Bug f5c4 made the two falsey states distinguishable at the source
(`GateState.UNREADABLE` vs `DISABLED`) while deliberately KEEPING the fail-OPEN posture,
which was a separate, contested policy question. Ticket 39f8-ae7c then carried that
question to an operator, whose ruling — "Unreadable config should result in an error" —
settled it: an unreadable config is no longer a state a gate can resolve TO at all; it
raises a `ConfigError` naming the gate, the ticket, and the parse fault.

These tests pin the post-ruling contract: a readable config resolves ENABLED/DISABLED
(bool-compatible, so the ~15 test modules monkeypatching `gate_enabled` with a plain bool
keep working), and an unreadable config raises — never a silent skip, never a fake
`disabled` verdict.
"""

from __future__ import annotations

import pathlib

import pytest

from rebar import config
from rebar._commands import gates
from rebar.config import ConfigError

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


def test_a_readable_config_resolves_to_its_own_named_state(cfg_root) -> None:
    """ENABLED and DISABLED are total and separately nameable by a caller."""
    assert _resolve(cfg_root(_ON)) is gates.GateState.ENABLED
    assert _resolve(cfg_root(_OFF)) is gates.GateState.DISABLED


def test_truthiness_is_unchanged_for_readable_configs(cfg_root) -> None:
    """Bool-compatibility is load-bearing: every existing caller and the ~15 test modules
    that monkeypatch this symbol with a plain bool must keep working."""
    assert bool(_resolve(cfg_root(_ON))) is True
    assert bool(_resolve(cfg_root(_OFF))) is False


def test_an_unreadable_config_raises_naming_the_gate_and_the_parse_fault(cfg_root) -> None:
    """The operator ruling (39f8): a config FAULT is an ERROR, never a resolvable state."""
    with pytest.raises(ConfigError) as excinfo:
        _resolve(cfg_root(_UNREADABLE))

    message = str(excinfo.value)
    assert "the plan-review close gate" in message, f"gate not named: {message!r}"
    assert "1111-2222-3333-4444" in message, f"ticket not named: {message!r}"
    assert excinfo.value.__cause__ is not None, "the parse fault was not chained"


def test_gate_state_has_no_unreadable_member() -> None:
    """UNREADABLE is not a state any caller can branch on any more — the enum's remaining
    job is the readable-config policy split plus bool compatibility."""
    assert {s.name for s in gates.GateState} == {"ENABLED", "DISABLED"}


def test_the_close_gate_propagates_the_config_fault_instead_of_a_skip_verdict(
    cfg_root,
) -> None:
    """RE-ANCHORED twice (f5c4 deferral -> 2091 verdict split -> 39f8 ruling).

    The close gate must not launder a config FAULT into anything — neither a
    `disabled` verdict (f5c4's bug) nor its own `unreadable` skip verdict (the 2091
    interim): it errors, and the disabled POLICY CHOICE keeps its honest skip payload.
    """
    state = {"ticket_id": "1111-2222-3333-4444", "ticket_type": "story", "status": "in_progress"}

    off = gates.close_plan_review_gate_check(state["ticket_id"], state, repo_root=cfg_root(_OFF))
    assert off["verdict"] == "disabled"
    assert off["ok"] is True
    assert off["gate_ran"] is False

    with pytest.raises(ConfigError):
        gates.close_plan_review_gate_check(
            state["ticket_id"], state, repo_root=cfg_root(_UNREADABLE)
        )
