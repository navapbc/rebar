"""A linked worktree reads its OWN rebar.toml, not the main checkout's.

Regression for nosophobic-zebraic-waterbug: from a linked worktree, the first-run store
link loaded and validated the main checkout's whole config (warning about keys only that
file had), and every ticket write took ``sync.push`` from the main checkout because the
push policy was resolved from the shared store's parent directory.
"""

from __future__ import annotations

import logging
import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from rebar import config
from rebar._commands import init as init_cmd
from rebar._store import push
from rebar._store.push_classify import PushDeliveryError

pytestmark = pytest.mark.unit

_MAIN_TOML = """\
[tracker]
dir = "custom-store"

[sync]
push = "always"

[llm]
main_only_unknown_key = 1
"""

_WORKTREE_TOML = """\
[sync]
push = "off"
"""


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def checkouts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Path, Path]]:
    """A main repo and a linked worktree, each carrying a different rebar.toml."""
    main = (tmp_path / "main").resolve()
    main.mkdir()
    _git(main, "init", "-q")
    _git(main, "config", "user.email", "t@e")
    _git(main, "config", "user.name", "T")
    (main / "README").write_text("x\n")
    _git(main, "add", "README")
    _git(main, "commit", "-q", "-m", "init")
    worktree = (tmp_path / "wt").resolve()
    _git(main, "worktree", "add", "-q", "-b", "wt", str(worktree))
    (main / "rebar.toml").write_text(_MAIN_TOML)
    (main / "custom-store").mkdir()
    (worktree / "rebar.toml").write_text(_WORKTREE_TOML)

    for name in ("REBAR_TRACKER_DIR", "REBAR_CONFIG", "REBAR_SYNC_PUSH"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("REBAR_ROOT", str(worktree))
    monkeypatch.chdir(worktree)
    config.reset_config_cache()
    yield main, worktree
    config.reset_config_cache()


@pytest.fixture
def config_warnings() -> Iterator[list[str]]:
    """Messages logged by ``rebar.config``, captured on that logger directly."""
    seen: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            seen.append(record.getMessage())

    logger = logging.getLogger("rebar.config")
    handler = _Capture(level=logging.WARNING)
    logger.addHandler(handler)
    try:
        yield seen
    finally:
        logger.removeHandler(handler)


def test_first_run_link_honours_main_tracker_dir_without_main_config_warnings(
    checkouts: tuple[Path, Path], config_warnings: list[str]
) -> None:
    main, worktree = checkouts

    assert init_cmd.pending_init_is_symlink(str(worktree)) is True
    found = init_cmd._main_worktree_tracker(str(worktree))

    assert found is not None
    assert os.path.realpath(found) == os.path.realpath(main / "custom-store")
    assert not [m for m in config_warnings if "unknown key" in m], config_warnings


def test_worktree_write_uses_worktree_sync_push(
    checkouts: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    main, _worktree = checkouts
    pushes: list[tuple[str, ...]] = []

    def fake_git(_base: str, *args: str, **_kwargs: object) -> subprocess.CompletedProcess:
        if args and args[0] == "push":
            pushes.append(args)
        out = "2\n" if args[:2] == ("rev-list", "--count") else ""
        return subprocess.CompletedProcess(args, 0, out, "")

    monkeypatch.setattr(push, "_git", fake_git)

    with pytest.raises(PushDeliveryError) as caught:
        push.push_tickets_branch(str(main / "custom-store"), strict=True)

    assert caught.value.reason == "push-disabled"
    assert pushes == []


def test_peer_tracker_dir_defaults_without_config(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    _git(root, "init", "-q")
    config.reset_config_cache()

    assert config.peer_tracker_dir(root) == root / ".tickets-tracker"


def test_peer_tracker_dir_keeps_absolute_tracker_dir(tmp_path: Path) -> None:
    root = (tmp_path / "repo").resolve()
    root.mkdir()
    _git(root, "init", "-q")
    elsewhere = (tmp_path / "store").resolve()
    (root / "rebar.toml").write_text(f'[tracker]\ndir = "{elsewhere}"\n')
    config.reset_config_cache()

    assert config.peer_tracker_dir(root) == elsewhere
