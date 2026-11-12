"""`rebar store fsck --only` help and parsing agree (albinistic-selenite-tick).

The help said "check only the named ticket(s)", but the command accepts exactly one value,
`stale-channel`, and only with `--repair`; and it rejected the space-separated spelling the
help (argparse) implies. These tests pin the help text and the parser together. Every case
fails before the store is read, so no tracker is needed.
"""

from __future__ import annotations

import pytest

from rebar._cli._parsers.core.repair import build_fsck
from rebar._commands.fsck import fsck_cli

pytestmark = pytest.mark.unit


def _only_help() -> str:
    parser = build_fsck(prog="rebar store fsck")
    action = next(a for a in parser._actions if "--only" in a.option_strings)
    return f"{action.help} {action.choices}"


def test_help_describes_what_the_command_accepts() -> None:
    text = _only_help()
    assert "stale-channel" in text
    assert "ticket" not in text


@pytest.mark.parametrize(
    "value",
    ["stale-channel", "77b6-868f-f4bd-461f"],
)
def test_space_and_equals_spellings_behave_the_same(
    value: str, capsys: pytest.CaptureFixture[str]
) -> None:
    rc_space = fsck_cli(["--only", value])
    err_space = capsys.readouterr().err
    rc_equals = fsck_cli([f"--only={value}"])
    err_equals = capsys.readouterr().err

    assert (rc_space, err_space) == (rc_equals, err_equals)
    assert rc_space == 2
    assert "requires a value" not in err_space


def test_unknown_value_names_the_accepted_one(capsys: pytest.CaptureFixture[str]) -> None:
    rc = fsck_cli(["--only", "77b6-868f-f4bd-461f"])
    err = capsys.readouterr().err

    assert rc == 2
    assert "unknown --only value '77b6-868f-f4bd-461f'" in err
    assert "stale-channel" in err


def test_bare_only_at_the_end_still_requires_a_value(capsys: pytest.CaptureFixture[str]) -> None:
    rc = fsck_cli(["--repair", "--only"])
    err = capsys.readouterr().err

    assert rc == 2
    assert "--only requires a value" in err
