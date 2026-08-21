""" "Did the close gate RUN?" must have ONE answer, owned by `gates` (bug 2091).

`transition_close` used to decide whether to install the in-lock recheck by comparing the
verdict string to `"disabled"`. That is only correct while `"disabled"` is the sole
non-running verdict — so splitting the verdict (this bug's other half) would silently start
doing config reads inside the write lock. Widening the comparison to a tuple is the same
fragility with one more element, so the payload carries an explicit stamp instead and
`gates.gate_ran` is the single reader of it.
"""

from __future__ import annotations

import pathlib

import pytest

from rebar import config
from rebar._commands import gates

_ATTR = "require_plan_review_for_close"
_OFF = f"[verify]\n{_ATTR} = false\n"
_UNREADABLE = f"[verify\n{_ATTR} = true\n"

_STATE = {"ticket_id": "1111-2222-3333-4444", "ticket_type": "story", "status": "in_progress"}


@pytest.fixture
def cfg_root(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()

    def write(text: str) -> str:
        pathlib.Path(root, "rebar.toml").write_text(text)
        config.reset_config_cache()
        return str(root)

    yield write
    config.reset_config_cache()


@pytest.mark.parametrize("cfg", [_OFF, _UNREADABLE], ids=["disabled", "unreadable"])
def test_neither_skip_verdict_counts_as_the_gate_having_run(cfg_root, cfg: str) -> None:
    """BOTH skip states must answer False — that is what keeps the in-lock recheck off."""
    check = gates.close_plan_review_gate_check(_STATE["ticket_id"], _STATE, repo_root=cfg_root(cfg))

    assert check["gate_ran"] is False
    assert gates.gate_ran(check) is False


def test_a_verdict_the_predicate_has_never_heard_of_cannot_change_close_behaviour() -> None:
    """AC4: the STAMP decides, not the verdict string.

    A future non-running verdict is invisible to the predicate as a *string* — it is
    carried by `gate_ran`, so adding one cannot silently start doing work under the write
    lock the way `!= "disabled"` did.
    """
    assert gates.gate_ran({"ok": True, "verdict": "some-future-skip", "gate_ran": False}) is False
    assert gates.gate_ran({"ok": True, "verdict": "some-future-run", "gate_ran": True}) is True

    # Symmetrically, the two historical skip strings carry no special power any more.
    assert gates.gate_ran({"ok": True, "verdict": "disabled", "gate_ran": True}) is True


def test_an_unstamped_payload_is_treated_as_not_having_run() -> None:
    """Fail-SAFE for the in-lock question: absent evidence, do no extra work under the lock."""
    assert gates.gate_ran({"ok": True, "verdict": "certified"}) is False
    assert gates.gate_ran({}) is False


def test_a_gate_that_ran_stamps_true(monkeypatch) -> None:
    """The stamp is not merely False-everywhere: a running gate reports True."""
    monkeypatch.setattr(gates, "gate_enabled", lambda *a, **k: True)

    # A ticket TYPE exemption still counts as the gate having RUN: it consulted config,
    # found the gate on, and reached its own verdict.
    check = gates.close_plan_review_gate_check(
        "1111-2222-3333-4444",
        {"ticket_id": "1111-2222-3333-4444", "ticket_type": "bug"},
        repo_root="/repo",
    )

    assert check["verdict"] == "exempt"
    assert check["gate_ran"] is True
    assert gates.gate_ran(check) is True
