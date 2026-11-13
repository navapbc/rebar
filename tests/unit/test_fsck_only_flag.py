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


def _rendered_help() -> str:
    """What `rebar store fsck --help` prints, flattened to single spaces."""
    return " ".join(build_fsck(prog="rebar store fsck").format_help().split())


def test_help_describes_what_the_command_accepts() -> None:
    text = _rendered_help()
    assert "--only {stale-channel}" in text
    assert "repair only stale-channel snapshots" in text
    assert "named ticket" not in text


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


def test_bare_only_followed_by_a_flag_still_requires_a_value(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = fsck_cli(["--only", "--repair"])
    err = capsys.readouterr().err

    assert rc == 2
    assert "--only requires a value" in err


@pytest.mark.parametrize(
    "argv",
    [
        ["--repair", "--only", "stale-channel", "--limit", "3"],
        ["--repair", "--only=stale-channel", "--limit=3"],
    ],
)
def test_accepted_spellings_reach_dispatch_with_the_same_values(
    monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    """The space spelling gets past validation to the same governed dispatch as `=`."""
    from rebar._commands import fsck

    seen: list[dict] = []

    def govern(**kwargs):
        seen.append(kwargs)
        return 0

    monkeypatch.setattr(fsck, "_fsck_govern", govern)

    assert fsck_cli(argv) == 0
    assert (seen[0]["only"], seen[0]["limit"], seen[0]["do_repair"]) == ("stale-channel", 3, True)


# ── unknown and value-less flags (grim-flavoured-bull) ──────────────────────────────────────
# fsck scans argv by hand and used to drop anything it did not recognise, so a typo exited 0:
# `--repair --dryrun` ran the REAL repair. Every rejected form must fail before dispatch.


def _no_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    from rebar._commands import fsck

    def _boom(*_a, **_k):
        raise AssertionError("fsck dispatched past argv validation")

    monkeypatch.setattr(fsck, "_fsck_govern", _boom)
    monkeypatch.setattr(fsck, "_repair_cli", _boom)


@pytest.mark.parametrize(
    ("argv", "named"),
    [
        (["--bogus"], "--bogus"),
        (["--repair", "--dryrun"], "--dryrun"),
        (["--repair", "extra"], "extra"),
        (["--only=stale-channel", "--repair", "--include-archive"], "--include-archive"),
    ],
)
def test_unknown_argument_is_rejected_before_dispatch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], argv, named
) -> None:
    _no_dispatch(monkeypatch)

    rc = fsck_cli(argv)
    err = capsys.readouterr().err

    assert rc == 2
    assert f"unknown option '{named}'" in err
    assert "Usage: rebar store fsck" in err


@pytest.mark.parametrize("argv", [["--limit"], ["--limit", "--repair"]])
def test_bare_limit_requires_a_value(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], argv
) -> None:
    _no_dispatch(monkeypatch)

    rc = fsck_cli(argv)
    err = capsys.readouterr().err

    assert rc == 2
    assert "--limit requires a value" in err


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["--include-archived"], {"include_archived": True}),
        (["--repair-snapshots"], {"repair_snapshots": True}),
        (["--repair", "--dry-run"], {"do_repair": True, "dry_run": True}),
        (["--limit", "1"], {"limit": 1}),
        (["--limit=1"], {"limit": 1}),
        (["--output", "json"], {"fmt": "json"}),
        (["--output=json"], {"fmt": "json"}),
        (["-o", "json"], {"fmt": "json"}),
    ],
)
def test_documented_spellings_still_reach_dispatch(
    monkeypatch: pytest.MonkeyPatch, argv, expected
) -> None:
    from rebar._commands import fsck

    seen: list[dict] = []

    def govern(**kwargs):
        seen.append(kwargs)
        return 0

    monkeypatch.setattr(fsck, "_fsck_govern", govern)

    assert fsck_cli(argv) == 0
    assert {k: seen[0][k] for k in expected} == expected
