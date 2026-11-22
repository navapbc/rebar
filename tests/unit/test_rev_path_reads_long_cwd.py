"""Every product ``<rev>:<path>`` read works when cwd plus the argument passes MAX_PATH.

Bug comic-heavyset-jerboa. ``git show <rev>:<path>`` without ``--`` stats the argument as a
possible filename, and on Windows that stat dies with "Filename too long" once
``<cwd>\\<rev>:<path>`` reaches MAX_PATH (260), even though the object exists. Each case
below commits what a product read looks for, checks it out as a ``--no-checkout`` linked
worktree (the shape of ``.tickets-tracker``) whose path is padded just past that limit for
the read's own argument, and drives the real function. The worktree's own ``.git`` file stays
under the limit, so only the ambiguity stat can fail. On POSIX the cases are ordinary
functional tests; on Windows they reproduce the bug.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from rebar._commands import _init_ensures, init, reclaim_publish
from rebar._engine_support import bridge_fsck
from rebar._opcert_gate_config import _committed_toml
from rebar._store import compat, project_ensures
from rebar.llm.evals import engine_comparison

pytestmark = pytest.mark.unit

_MAX_PATH = 260
#: How far past MAX_PATH ``cwd + 1 + argument`` is pushed. A few characters of margin keep the
#: case clear of any off-by-one in how git measures the joined path.
_OVER = 4
_TID = "a1b2-c3d4-e5f6-0718"
_EVENT = f"{_TID}/1795261069748668001-36d385c1-0b03-4cdd-b64b-de7e466e93d8-FUTURE.json"
_RECORD = {
    "format_version": compat.CURRENT_FORMAT_VERSION,
    "required_capabilities": [],
    "epoch": "epoch-2",
    "epoch_lineage": [None],
}
_FILES = {
    ".gitattributes": "* merge=ours\n",
    ".gitignore": _init_ensures._GITIGNORE,
    ".pre-commit-config.yaml": "repos: []\n",
    compat.COMPAT_FILENAME: json.dumps(_RECORD),
    project_ensures._PROJECTS_REL_PATH: '{"version": 1, "legacy_default": "", "projects": {}}',
    "rebar.toml": '[ticket]\ndefault_assignee = "someone@example.com"\n',
    _EVENT: '{"event_type": "FUTURE_EVENT_TYPE", "uuid": "x"}\n',
}


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.email=t@t", "-c", "user.name=T", *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


def _deep_tracker(tmp_path: Path, argument: str) -> Path:
    """A ``--no-checkout`` worktree of a ``tickets`` branch holding ``_FILES``, at a path
    exactly ``_OVER`` characters too long for ``<path>\\<argument>`` to fit in MAX_PATH."""
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "tickets")
    for rel, body in _FILES.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body.encode("utf-8"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed")
    _git(repo, "checkout", "-q", "-b", "main")
    base = tmp_path / "w"
    width = _MAX_PATH + _OVER - 1 - len(argument) - len(str(base)) - 1 - len("\\trk")
    assert width > 0, f"pytest temp dir too long to build the MAX_PATH case: {tmp_path}"
    tracker = base / ("x" * width) / "trk"
    tracker.parent.mkdir(parents=True)
    _git(repo, "worktree", "add", "-q", "--no-checkout", str(tracker), "tickets")
    assert len(str(tracker)) + 1 + len(argument) >= _MAX_PATH
    assert len(str(tracker / ".git")) < _MAX_PATH - 8
    return tracker


def _head(tracker: Path) -> str:
    return _git(tracker, "rev-parse", "HEAD")


def _ensure_unit(unit: Callable[[str], object]) -> Callable[[Path], None]:
    def check(tracker: Path) -> None:
        before = _head(tracker)
        outcome = unit(str(tracker))
        assert getattr(outcome, "status", None) == "ok", outcome
        assert _head(tracker) == before, "a converged ensure unit must not commit"

    return check


def _commit_precommit(tracker: Path) -> None:
    before = _head(tracker)
    init._commit_precommit(str(tracker))
    assert _head(tracker) == before, "the committed pre-commit config was misread as absent"


def _record_at(tracker: Path) -> None:
    assert reclaim_publish._record_at(tracker, _head(tracker)) == _RECORD


def _unknown_event_types(tracker: Path) -> None:
    assert bridge_fsck._unknown_event_types_from_ref(tracker) == ["FUTURE_EVENT_TYPE"]


def _committed_project_toml(tracker: Path) -> None:
    table = _committed_toml(tracker, "tickets:rebar.toml")
    assert table == {"ticket": {"default_assignee": "someone@example.com"}}


def _remote_store_epoch(tracker: Path) -> None:
    assert compat._remote_store_epoch(tracker, "tickets") == ("epoch-2", None)


def _remote_lineage_names(tracker: Path) -> None:
    assert compat._remote_lineage_names(tracker, "tickets", None) is True


def _remote_epoch_supersedes_local(tracker: Path) -> None:
    assert compat.remote_epoch_supersedes_local(tracker, _head(tracker)) is True


def _fixture_files(tracker: Path) -> None:
    row = {"file_impact": [{"path": compat.COMPAT_FILENAME}]}
    files, missing = engine_comparison._fixture_files(row, tracker)
    assert missing == 0
    assert json.loads(files[compat.COMPAT_FILENAME]) == _RECORD


_SHA = "0" * 40  # the width of the commit id these reads are given

#: (read, the argument it hands git, the check that drives it)
_CASES: list[tuple[str, str, Callable[[Path], None]]] = [
    (
        "gitattributes-unit",
        "tickets:.gitattributes",
        _ensure_unit(_init_ensures._gitattributes_unit),
    ),
    ("gitignore-unit", "tickets:.gitignore", _ensure_unit(_init_ensures._gitignore_unit)),
    (
        "store-compat-unit",
        f"tickets:{compat.COMPAT_FILENAME}",
        _ensure_unit(_init_ensures._store_compat_unit),
    ),
    (
        "projects-seed-unit",
        f"tickets:{project_ensures._PROJECTS_REL_PATH}",
        _ensure_unit(project_ensures.seed_projects_mapping_unit),
    ),
    ("commit-precommit", "tickets:.pre-commit-config.yaml", _commit_precommit),
    ("reclaim-record-at", f"{_SHA}:{compat.COMPAT_FILENAME}", _record_at),
    ("bridge-fsck-candidate", f"{bridge_fsck._TICKETS_REF}:{_EVENT}", _unknown_event_types),
    ("opcert-committed-toml", "tickets:rebar.toml", _committed_project_toml),
    ("compat-remote-epoch", f"tickets:{compat.COMPAT_FILENAME}", _remote_store_epoch),
    ("compat-remote-lineage", f"tickets:{compat.COMPAT_FILENAME}", _remote_lineage_names),
    ("compat-supersedes-local", f"{_SHA}:{compat.COMPAT_FILENAME}", _remote_epoch_supersedes_local),
    ("engine-fixture-files", f"HEAD:{compat.COMPAT_FILENAME}", _fixture_files),
]


@pytest.mark.parametrize(("argument", "check"), [c[1:] for c in _CASES], ids=[c[0] for c in _CASES])
def test_the_read_finds_the_committed_object_past_max_path(
    tmp_path: Path, argument: str, check: Callable[[Path], None]
) -> None:
    check(_deep_tracker(tmp_path, argument))
