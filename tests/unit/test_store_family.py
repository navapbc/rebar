"""The ``rebar store <sub>`` storage-maintenance family (story fea5-698b).

Nine top-level spellings (``fsck``, ``fsck-recover``, ``doctor``, ``tracker-maintenance``,
``tracker-footprint``, ``export``, ``import``, ``compact``, ``compact-all``) moved under one
``store`` route. These pin the family's contract through the real dispatcher:

* the family's own usage errors and help;
* each member keeps the init policy and mount behaviour it had at top level;
* ``store compact`` selects the one-ticket arm or the ``--all`` sweep, and a sweep-only
  option without ``--all`` is a usage error;
* the old spellings resolve to nothing (their hints are pinned in
  ``test_cli_registry_dispatch.py``, parametrized from ``_REMOVED_SPELLINGS``).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
from rebar._cli import _help, _registry, main
from rebar._cli._parsers.core.repair import build_fsck

pytestmark = pytest.mark.unit

_OLD_SPELLINGS = (
    "fsck",
    "fsck-recover",
    "doctor",
    "tracker-maintenance",
    "tracker-footprint",
    "export",
    "import",
    "compact",
    "compact-all",
)


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A real initialized rebar store bound as ``REBAR_ROOT`` for in-process main()."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "t@t.co"),
        ("config", "user.name", "t"),
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    monkeypatch.setenv("REBAR_SYNC_PUSH", "off")
    rebar.init_repo(repo_root=str(repo))
    return repo


# --- the family's own surface --------------------------------------------------------


@pytest.mark.parametrize("old", _OLD_SPELLINGS)
def test_old_spelling_is_not_a_route(old: str) -> None:
    assert _registry.route_for(old) is None


def test_bare_store_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["store"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("Usage: rebar store <subcommand>")


def test_unknown_member_is_an_invalid_choice(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["store", "bogus"]) == 2
    err = capsys.readouterr().err
    assert "rebar store: error:" in err
    assert "invalid choice: 'bogus'" in err


def test_store_help_is_the_committed_artifact(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["store", "--help"]) == 0
    assert capsys.readouterr().out == _help.subcommand_help("store")


def test_member_help_renders_the_member_factory(capsys: pytest.CaptureFixture[str]) -> None:
    expected = build_fsck(prog="rebar store fsck").format_help()
    expected = "Usage: " + expected[len("usage: ") :]
    assert main(["store", "fsck", "--help"]) == 0
    assert capsys.readouterr().out == expected.rstrip("\n") + "\n"


def test_every_member_resolves_handler_and_factory() -> None:
    import argparse
    import importlib

    for route in _registry.STORE_ROUTES:
        assert route.handler and route.parser_factory, route.name
        module, _, attr = route.handler.partition(":")
        assert callable(getattr(importlib.import_module(module), attr)), route.name
        module, _, attr = route.parser_factory.partition(":")
        factory = getattr(importlib.import_module(module), attr)
        assert isinstance(factory(prog=f"rebar store {route.name}"), argparse.ArgumentParser)


# --- each member keeps its own init policy and mount behaviour -------------------------

_MEMBER_INIT = [
    # (member, argv after the member, expected ensure_initialized(init_only=...) calls)
    ("fsck", [], [False]),
    ("doctor", [], [True]),
    ("doctor", ["--repair"], [False]),
    ("maintenance", ["--status"], [False]),
    ("footprint", [], []),
    ("export", [], [True]),
    ("import", [], [False]),
    ("compact", ["--all"], [False]),
]


@pytest.mark.parametrize(
    ("member", "argv", "expected"),
    _MEMBER_INIT,
    ids=[f"{m}-{i}" for i, (m, *_r) in enumerate(_MEMBER_INIT)],
)
def test_member_runs_under_its_own_init_policy(
    member: str, argv: list[str], expected: list[bool], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dispatch through ``store_cli`` (the family handler) and spy on initialization."""
    from rebar._cli._store_commands import store_cli

    route = _registry.route_for(member, _registry.STORE_ROUTES)
    assert route is not None and route.handler is not None
    calls: list[bool] = []
    monkeypatch.setattr(
        "rebar._cli.ensure_initialized", lambda *, init_only: calls.append(init_only)
    )
    monkeypatch.setattr(route.handler.replace(":", "."), lambda *_a, **_k: 0)
    assert store_cli([member, *argv]) == 0
    assert calls == expected


def test_fsck_recover_member_skips_init_under_an_injected_tracker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from rebar._cli._store_commands import store_cli

    calls: list[bool] = []
    monkeypatch.setattr(
        "rebar._cli.ensure_initialized", lambda *, init_only: calls.append(init_only)
    )
    monkeypatch.setattr("rebar._commands.fsck_recover.fsck_recover_cli", lambda *_a, **_k: 0)
    monkeypatch.setattr("rebar.config.tracker_dir_override", lambda: "/injected/tracker")
    assert store_cli(["fsck-recover"]) == 0
    assert calls == []


def test_only_no_auto_mount_members_skip_the_central_mount() -> None:
    from rebar._cli import _store_mount_eligible

    assert _store_mount_eligible(["store", "footprint"]) is False
    assert _store_mount_eligible(["store", "fsck"]) is True
    assert _store_mount_eligible(["store", "footprint", "--help"]) is False


# --- store compact: one ticket, or the sweep under --all ----------------------------------


def test_compact_dispatch_selects_the_arm(monkeypatch: pytest.MonkeyPatch) -> None:
    from rebar._commands import compact

    seen: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        compact, "compact_all_cli", lambda argv, **_k: seen.append(("all", argv)) or 0
    )
    monkeypatch.setattr(compact, "compact_cli", lambda argv, **_k: seen.append(("one", argv)) or 0)

    assert compact.compact_dispatch_cli(["--all", "--dry-run", "--limit=2"]) == 0
    assert compact.compact_dispatch_cli(["--dry-run", "--all"]) == 0
    assert compact.compact_dispatch_cli(["tid", "--threshold=0"]) == 0
    # ``--all`` after ``--`` is a ticket-id argument, not the sweep selector.
    assert compact.compact_dispatch_cli(["--", "--all"]) == 0
    assert seen == [
        ("all", ["--dry-run", "--limit=2"]),
        ("all", ["--dry-run"]),
        ("one", ["tid", "--threshold=0"]),
        ("one", ["--", "--all"]),
    ]


@pytest.mark.parametrize("flag", ["--dry-run", "--limit=3", "--include-archived"])
def test_sweep_only_option_without_all_is_a_usage_error(
    flag: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from rebar._commands import compact

    monkeypatch.setattr(compact, "compact_cli", lambda *_a, **_k: pytest.fail("one-ticket arm ran"))
    assert compact.compact_dispatch_cli(["tid", flag]) == 2
    assert "rebar store compact --all" in capsys.readouterr().err


def test_store_compact_all_dry_run_matches_the_sweep(
    store: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``store compact --all --dry-run`` prints exactly what the sweep entry prints."""
    from rebar._commands import compact

    rebar.create_ticket("task", "one", repo_root=str(store))
    assert compact.compact_all_cli(["--dry-run"]) == 0
    direct = capsys.readouterr().out
    assert main(["store", "compact", "--all", "--dry-run"]) == 0
    assert capsys.readouterr().out == direct
    assert "Tickets needing compaction" in direct
