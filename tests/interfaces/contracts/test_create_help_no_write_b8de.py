"""A ``--help`` request must never be stored as ticket content (bug b8de).

``rebar create task --help`` (a request to view the create usage) used to create a
placeholder ticket titled ``--help``: the dispatcher only honoured ``--help``/``-h`` when
it was the FIRST token after the subcommand (``rest[0]``), so with ``rest == ["task",
"--help"]`` the flag fell through to ``composer.create_cli`` and was consumed as the title,
performing a CREATE write.

The repaired contract: ``--help``/``-h`` appearing anywhere before a ``--`` terminator is a
help request for a routable subcommand — it prints the pinned usage to stdout, exits 0, and
performs NO write. ``--`` remains the escape hatch, and the commands that own their own
help (and the unknown-subcommand error) are unchanged.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
from rebar import _cli

pytestmark = pytest.mark.interface


@pytest.fixture
def rebar_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("init",),
        ("config", "user.email", "test@example.com"),
        ("config", "user.name", "Test"),
    ):
        subprocess.run(["git", *args], cwd=repo, check=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    return repo


def _ticket_count(repo: Path) -> int:
    return len(rebar.list_tickets(repo_root=str(repo)))


def test_create_task_help_shows_usage_and_writes_nothing(
    rebar_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The reported repro: ``create task --help`` prints usage, exits 0, creates NO ticket."""
    assert _ticket_count(rebar_repo) == 0

    rc = _cli.main(["create", "task", "--help"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "create" in out.lower()
    assert _ticket_count(rebar_repo) == 0, "a --help request must not create a ticket"


@pytest.mark.parametrize(
    "argv",
    [
        ["create", "task", "--help"],
        ["create", "task", "-h"],
        ["create", "task", "a title", "--help"],
        ["edit", "some-id", "--help"],
    ],
)
def test_help_anywhere_before_terminator_shows_usage_no_write(
    rebar_repo: Path, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    """``--help``/``-h`` in any pre-``--`` position triggers usage and no write."""
    before = _ticket_count(rebar_repo)

    rc = _cli.main(argv)

    assert rc == 0
    assert capsys.readouterr().out.strip(), "expected usage text on stdout"
    assert _ticket_count(rebar_repo) == before, "help must not mutate the store"


def test_flat_bridge_family_serves_pinned_help_for_nonleading_flag(
    rebar_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A FLAT compatibility command in the ``bridge`` group owns no nested children, so a
    non-leading ``--help`` is ITS OWN usage request and must be served from the pinned,
    capitalized artifact instead of falling through to argparse's lowercase ``usage: rebar``
    with the wrong program name. Regression for the ``_NESTED_FAMILY`` over-inclusion
    (deriving the set from
    ``group == "bridge"`` wrongly swept in the flat, non-nested arm)."""
    from rebar._cli._registry import ROUTES

    flat = next(
        r.name for r in ROUTES if r.group == "bridge" and not r.hidden and r.name != "bridge"
    )
    rc = _cli.main([flat, "somearg", "--help"])

    out = capsys.readouterr().out
    assert rc == 0
    assert out.startswith(f"Usage: rebar {flat}"), out[:80]


@pytest.mark.parametrize("child", ["create", "use", "key"])
def test_identity_child_parser_owns_help(child: str, capsys: pytest.CaptureFixture[str]) -> None:
    """Each identity child parser renders its own help and exits successfully."""
    from rebar._cli._parsers.advanced.identity import build

    parser = build(prog="rebar identity")
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args([child, "--help"])

    assert exc_info.value.code == 0
    first_line = capsys.readouterr().out.splitlines()[0]
    assert first_line.startswith(f"usage: rebar identity {child}")


def test_config_validate_handler_owns_child_help(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The config validate handler renders child help and exits successfully."""
    with pytest.raises(SystemExit) as exc_info:
        _cli.main(["config", "validate", "--help"])

    assert exc_info.value.code == 0
    first_line = capsys.readouterr().out.splitlines()[0]
    assert first_line.startswith("usage: rebar config validate")


def _help_backed_nested_families() -> list[tuple[str, str]]:
    """Return every child of each live visible route with populated subparsers."""
    import argparse
    import importlib

    from rebar._cli._registry import ROUTES

    out: list[tuple[str, str]] = []
    for r in ROUTES:
        help_backed = not r.hidden and not r.retired
        if not (help_backed and r.parser_factory):
            continue
        module_name, attr = r.parser_factory.split(":")
        parser = getattr(importlib.import_module(module_name), attr)(prog=f"rebar {r.name}")
        subs = next((a for a in parser._actions if isinstance(a, argparse._SubParsersAction)), None)
        if subs is not None and subs.choices:
            out.extend((r.name, child) for child in sorted(subs.choices))
    return out


def test_nested_family_child_help_falls_through_to_argparse(
    rebar_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A HELP-BACKED nested family owns its own subcommand parsing, so a NON-leading ``--help``
    (``audit serve --help``, ``bridge preview --help``) is the CHILD's usage request and must
    fall through to the real parser — NOT be intercepted to the family's pinned top-level
    artifact (which would show e.g. ``rebar audit [-h] {show,serve}`` instead of the child's
    own options). Iterates the whole class so a future nested family omitted from
    ``_NESTED_FAMILY`` also trips this. Regression: ``_NESTED_FAMILY`` was derived only from the
    ``bridge`` parser factory, so ``audit``'s children were wrongly swept into the pre-scan."""
    from rebar._cli import _help_route

    families = _help_backed_nested_families()
    assert families, "expected at least the bridge and audit nested families"
    family_names = {command for command, _child in families}
    expected_names = _help_route._NESTED_FAMILY - _help_route._HIDDEN_ALIASES - {"config"}
    assert family_names == expected_names
    assert _help_route._NESTED_INTERCEPTS == frozenset(
        {"audit", "config", "criteria", "identity", "llm", "prompt", "workflow"}
    )

    for cmd, child in families:
        capsys.readouterr()
        try:
            rc: object = _cli.main([cmd, child, "--help"])
        except SystemExit as exc:  # a fall-through argparse --help may exit rather than return
            rc = exc.code
        out = capsys.readouterr().out
        assert rc == 0, (cmd, child, rc)
        assert f"{cmd} {child}" in out.splitlines()[0], (cmd, child, out[:120])


def test_config_validate_child_help_reaches_its_parser(
    rebar_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Config validate help comes from the handler-owned child parser."""
    from rebar._cli._parsers.advanced.config import build_validate

    expected = build_validate(prog="rebar config validate").format_help()
    try:
        rc: object = _cli.main(["config", "validate", "--help"])
    except SystemExit as exc:
        rc = exc.code

    streams = capsys.readouterr()
    assert rc == 0
    assert streams.out == expected
    assert streams.err == ""


def test_help_behind_a_config_prefix_is_served_without_write(
    rebar_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A leading ``-c SECTION.KEY=VALUE`` config prefix is stripped by the pre-scan, so the
    help request behind it is still served (exit 0, usage on stdout) and mutates nothing."""
    before = _ticket_count(rebar_repo)

    rc = _cli.main(["-c", "ticket.default_assignee=x@y", "create", "--help"])

    out = capsys.readouterr().out
    assert rc == 0
    assert out.startswith("Usage: rebar create"), out[:80]
    assert _ticket_count(rebar_repo) == before, "help behind a config prefix must not write"


def test_malformed_config_prefix_falls_through_to_the_real_cli() -> None:
    """A MALFORMED leading ``-c``/``--config`` token makes the pre-scan decline (return
    ``None``) so the real config parser produces its exact error, rather than the pre-scan
    guessing at a help form. A well-formed prefix, by contrast, is stripped and the residual
    help request is handled (a non-``None`` exit code)."""
    from rebar._cli import _help_route

    # missing value, non SECTION.KEY=VALUE value, and the ``--config=`` spelling
    assert _help_route.pre_scan(["-c"]) is None
    assert _help_route.pre_scan(["-c", "not-a-kv", "create", "--help"]) is None
    assert _help_route.pre_scan(["--config", "-x", "create", "--help"]) is None
    assert _help_route.pre_scan(["--config=not-a-kv", "list"]) is None

    # a WELL-FORMED prefix is stripped and the residual request IS handled
    assert _help_route.pre_scan(["-c", "a.b=c", "--help"]) is not None


def test_ordinary_create_still_writes(rebar_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A real title with no help flag creates exactly one ticket (no over-eager intercept)."""
    rc = _cli.main(["create", "task", "a real title"])
    capsys.readouterr()

    assert rc == 0
    assert _ticket_count(rebar_repo) == 1


@pytest.mark.parametrize(
    ("rest", "expected"),
    [
        (["--help"], True),
        (["-h"], True),
        (["task", "--help"], True),
        (["task", "a title", "-h"], True),
        ([], False),
        (["task", "a real title"], False),
        (["--", "--help"], False),
        (["task", "--", "-h"], False),
        (["--help", "--"], True),
    ],
)
def test_wants_help_scans_before_the_terminator(rest: list[str], expected: bool) -> None:
    """The dispatcher help scan honours a flag in any position up to a ``--`` terminator."""
    assert _cli._wants_help(rest) is expected


@pytest.mark.parametrize(
    ("sub", "rest", "expected"),
    [
        # Normal (leaf) commands: help flag in any pre-`--` position is a request.
        ("create", ["task", "--help"], True),
        ("edit", ["some-id", "-h"], True),
        ("create", ["task", "a title"], False),
        # bridge* own nested help: only a LEADING flag asks for the family usage,
        # a later flag belongs to a child (`bridge preview --help`).
        ("bridge", ["--help"], True),
        ("bridge", ["preview", "--help"], False),
        ("bridge", ["sync", "-h"], False),
        ("bridge", ["status"], False),
    ],
)
def test_help_requested_respects_nested_dispatch(sub: str, rest: list[str], expected: bool) -> None:
    """Nested-dispatch families honour only a leading flag; leaf commands scan any position."""
    assert _cli._help_requested(sub, rest) is expected
