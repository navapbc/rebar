"""Regression: `rebar list --status=<invalid>` must ERROR, not silently return [].

Bug spiny-ferry-ripen (2dce-d7b9-6574-4f62): an unrecognized ``--status`` value
(e.g. ``all``) fell through to the reducer's set-membership filter, matched no
ticket, and returned an empty list with exit 0 — indistinguishable from "no
tickets match". An invalid filter value must be a loud, non-zero error that
names the valid statuses.
"""

from __future__ import annotations

import pytest

from rebar._engine_support.reads_cli import _cmd_list


@pytest.mark.unit
@pytest.mark.parametrize("bad", ["all", "bogus", "Open", "in-progress", "open,all"])
def test_list_invalid_status_errors(tmp_path, capsys: pytest.CaptureFixture[str], bad: str) -> None:
    """An invalid --status value exits non-zero and names it + the valid set.

    Validation fires before any tracker access, so a bare tmp dir is enough.
    """
    rc = _cmd_list([f"--status={bad}"], str(tmp_path))
    err = capsys.readouterr().err
    assert rc != 0, f"--status={bad!r} should be a non-zero error, got exit {rc}"
    # The offending value and the valid vocabulary are both surfaced.
    assert "status" in err.lower()
    assert "open" in err and "in_progress" in err and "closed" in err


@pytest.mark.unit
@pytest.mark.parametrize(
    "good",
    ["idea", "open", "in_progress", "closed", "blocked", "archived", "deleted", "open,closed"],
)
def test_list_valid_status_not_rejected(
    tmp_path, capsys: pytest.CaptureFixture[str], good: str
) -> None:
    """Every genuinely-valid status (incl. comma-OR) passes validation.

    With no initialized tracker the command still errors later, but NOT with the
    invalid-status message — proving valid values clear the new gate.
    """
    _cmd_list([f"--status={good}"], str(tmp_path))
    err = capsys.readouterr().err
    assert "invalid --status" not in err.lower()
