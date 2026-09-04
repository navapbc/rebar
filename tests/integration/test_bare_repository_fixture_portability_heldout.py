"""Held-out oracle for the bare-repository discovery lint.

Ticket 3476-4472-3407-4246 fixed 13 files that addressed a bare fixture remote
with ``git -C <bare>`` -- which git refuses under ``safe.bareRepository =
explicit`` -- and left this file behind as the guard. It pinned a HAND-MAINTAINED
allowlist of three test node ids and re-ran them under a strict git config.
Four days later ``tests/interfaces/lifecycle/test_atomic_completion_close.py``
landed, reintroduced the construct at eight sites, and nothing failed: the
allowlist named three files that were ALREADY FIXED. Coverage that must be
hand-registered is coverage a new file escapes by default
[rebar:740d-187c-53a2-4b7d].

So the allowlist is gone. Coverage now comes from
``scripts/check_bare_repository_discovery.py``, which sweeps every
``tests/**/*.py`` module in the tree, and this file is that sweep's oracle: it
asserts the tree is clean, keeps the detector honest against synthetic modules
so it cannot rot into a tautology, and pins the git behaviour the lint's rule
encodes.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
from _subprocess_env import subprocess_env

pytestmark = pytest.mark.integration

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "check_bare_repository_discovery.py"

_OFFENDER = (
    "import subprocess\n"
    "def _git(repo, *args):\n"
    '    return subprocess.run(["git", "-C", str(repo), *args], check=True)\n'
    "def test_reads_remote(tmp_path):\n"
    '    remote = tmp_path / "remote.git"\n'
    '    _git(tmp_path, "init", "--bare", str(remote))\n'
    '    _git(remote, "show-ref")\n'
    '    _git(remote.parent, "status")\n'
)


@pytest.fixture(scope="module")
def lint() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_bare_repository_discovery", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_bare_repository_discovery"] = module
    spec.loader.exec_module(module)
    return module


def test_no_test_addresses_a_bare_repository_by_discovery(lint: ModuleType) -> None:
    """Coverage follows the tree: every ``tests/**/*.py`` module is in scope."""
    findings = lint.check(_REPO_ROOT)

    assert not findings, "\n".join(["", *(str(finding) for finding in findings)])


def test_the_lint_detects_a_reintroduced_discovery_site(lint: ModuleType) -> None:
    """The guard has teeth: a fresh bare-fixture module in the banned shape fails."""
    fixed = _OFFENDER.replace('_git(remote, "show-ref")', '_bare_git(remote, "show-ref")')

    findings = lint.scan_module(Path("tests/test_offender.py"), _OFFENDER)

    assert [(f.line, f.name, f.via) for f in findings] == [(7, "remote", "_git()")]
    assert lint.scan_module(Path("tests/test_offender.py"), fixed) == []


def test_the_lint_follows_a_returned_bare_remote_through_a_rename(lint: ModuleType) -> None:
    """A caller may bind a fixture's bare remote under any name and still be caught."""
    module = (
        "import subprocess\n"
        "def _git(repo, *args):\n"
        '    return subprocess.run(["git", "-C", str(repo), *args], check=True)\n'
        "def _ticket_remote(tmp_path):\n"
        '    remote = tmp_path / "remote.git"\n'
        '    writer = tmp_path / "writer"\n'
        '    _git(tmp_path, "init", "--bare", str(remote))\n'
        "    return remote, writer\n"
        "def test_reads_remote(tmp_path):\n"
        "    origin, scribe = _ticket_remote(tmp_path)\n"
        '    _git(origin, "show-ref")\n'
        '    _git(scribe, "status")\n'
    )

    findings = lint.scan_module(Path("tests/test_renamed.py"), module)

    # Only the returned BARE element is flagged; the worktree beside it is not.
    assert [(f.line, f.name) for f in findings] == [(11, "origin")]


def test_the_lint_follows_a_bare_repository_passed_into_a_fixture(lint: ModuleType) -> None:
    """A parameter the callee creates a bare repository at marks the caller's argument."""
    module = (
        "import subprocess\n"
        "def _git(repo, *args):\n"
        '    return subprocess.run(["git", "-C", str(repo), *args], check=True)\n'
        "def _publish(repo, target):\n"
        '    _git(target.parent, "init", "--bare", str(target))\n'
        "def test_reads_remote(tmp_path, project):\n"
        '    somewhere = tmp_path / "somewhere.git"\n'
        "    _publish(project, somewhere)\n"
        '    _git(somewhere, "show-ref")\n'
    )

    findings = lint.scan_module(Path("tests/test_passed.py"), module)

    assert [(f.line, f.name) for f in findings] == [(9, "somewhere")]


def test_the_lint_honours_the_inline_safe_bare_repository_opt_in(lint: ModuleType) -> None:
    """An explicit ``-c safe.bareRepository=all`` is a deliberate escape, not a defect."""
    module = (
        "import subprocess\n"
        "def _git(repo, *args):\n"
        '    return subprocess.run(["git", "-C", str(repo), *args], check=True)\n'
        "def test_reads_remote(tmp_path):\n"
        '    remote = tmp_path / "remote.git"\n'
        '    _git(tmp_path, "init", "--bare", str(remote))\n'
        '    _git(remote, "-c", "safe.bareRepository=all", "rev-parse", "HEAD")\n'
    )

    assert lint.scan_module(Path("tests/test_escaped.py"), module) == []


def test_git_refuses_bare_discovery_but_accepts_an_explicit_git_dir(tmp_path: Path) -> None:
    """Pin the git behaviour the lint encodes, so the rule cannot rot silently."""
    strict_config = tmp_path / "strict-gitconfig"
    subprocess.run(
        ["git", "config", "--file", str(strict_config), "safe.bareRepository", "explicit"],
        check=True,
        capture_output=True,
        text=True,
    )
    environment = subprocess_env(
        GIT_CONFIG_GLOBAL=str(strict_config),
        GIT_CONFIG_NOSYSTEM="1",
    )
    bare = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "--quiet", str(bare)],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )

    def run(*argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *argv], env=environment, capture_output=True, text=True, check=False
        )

    discovered = run("-C", str(bare), "rev-parse", "--is-bare-repository")
    explicit = run("--git-dir", str(bare), "rev-parse", "--is-bare-repository")
    missing = run("--git-dir", str(tmp_path / "missing.git"), "rev-parse", "HEAD")

    assert discovered.returncode == 128
    assert "safe.bareRepository" in discovered.stderr
    assert explicit.returncode == 0
    assert explicit.stdout.strip() == "true"
    assert missing.returncode == 128
    assert "not a git repository" in missing.stderr
