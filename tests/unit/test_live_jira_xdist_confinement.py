"""Live-Jira test isolation must not depend on a distribution mode nobody configures.

Bug 06f4-1c04-83c8-4a9f. `pytest-xdist` honours ``@pytest.mark.xdist_group`` in exactly ONE
scheduler: ``--dist loadgroup``. Under ``load`` (xdist's default) and under ``worksteal`` (what
CI's default tier and ``docs/coverage.md``'s local command both use) the mark is parsed and then
DISCARDED — silently, with no error and no warning. Story 8d36 introduced xdist parallelism on
the invariant "live tests are kept serial or xdist_group-confined ... the command must not
assume [they self-skip]", so the confinement has to hold wherever those tests can actually run.

Two guards here, one per way that invariant broke:

* :func:`test_every_live_jira_test_is_xdist_group_confined` — a static policy scan: a test that
  gates on ``acli`` being on PATH is a live-Jira test and MUST carry the group. This is the guard
  that would have caught ``test_reconcile_dry_run_against_live_jira`` shipping without one.
* the subprocess guards — collection must FAIL FAST when group-confined live-Jira tests are
  collected under ``-n>0`` with a scheduler that ignores groups AND live credentials are present.
  They use ``--collect-only``, so no test body runs and no Jira traffic is ever issued.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest
from _subprocess_env import subprocess_env

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TESTS_DIR = _REPO_ROOT / "tests"
_EXTERNAL_DIR = _TESTS_DIR / "external"
_LIVE_JIRA_GROUP = "live_reconcile_e2e"

# The live-Jira E2E module is the confinement's reference member: the subprocess guards collect
# it (never run it) to prove the guard fires on a real, group-marked selection.
_LIVE_MODULE = "tests/integration/test_reconcile_live_e2e.py"


# --------------------------------------------------------------------------- #
# (b) Static policy scan — every live-Jira test carries the group mark
# --------------------------------------------------------------------------- #


def _is_acli_on_path_probe(node: ast.AST) -> bool:
    """True for a ``which("acli")`` / ``shutil.which("acli")`` call anywhere under *node*.

    Gating on the `acli` binary is the unambiguous signature of a test that talks to the real
    Jira: it is the only way any non-external test reaches the live instance.
    """
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name != "which":
            continue
        if any(isinstance(a, ast.Constant) and a.value == "acli" for a in child.args):
            return True
    return False


def _carries_group(decorators: list[ast.expr]) -> bool:
    """True if *decorators* contains ``@pytest.mark.xdist_group("live_reconcile_e2e")``."""
    for dec in decorators:
        if not isinstance(dec, ast.Call):
            continue
        func = dec.func
        if not (isinstance(func, ast.Attribute) and func.attr == "xdist_group"):
            continue
        if any(isinstance(a, ast.Constant) and a.value == _LIVE_JIRA_GROUP for a in dec.args):
            return True
    return False


def _module_is_group_confined(tree: ast.Module) -> bool:
    """True if a module-level ``pytestmark`` puts the whole module in the live group."""
    for node in tree.body:
        targets = (
            node.targets
            if isinstance(node, ast.Assign)
            else [node.target]
            if isinstance(node, ast.AnnAssign)
            else []
        )
        if not any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in targets):
            continue
        value = node.value
        marks = value.elts if isinstance(value, ast.List | ast.Tuple) else [value]
        if value is not None and _carries_group([m for m in marks if m is not None]):
            return True
    return False


def _unconfined_live_jira_tests() -> list[str]:
    """Return ``path::name`` for every live-Jira test not confined to the group."""
    offenders: list[str] = []
    for path in sorted(_TESTS_DIR.rglob("test_*.py")):
        if path.is_relative_to(_EXTERNAL_DIR):
            continue  # tests/external/ is confined to its own credential-scoped job
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):  # pragma: no cover - unreadable file is not this gate
            continue
        if not _is_acli_on_path_probe(tree):
            continue
        if _module_is_group_confined(tree):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if not node.name.startswith("test_"):
                continue
            if _is_acli_on_path_probe(node) and not _carries_group(node.decorator_list):
                offenders.append(f"{path.relative_to(_REPO_ROOT)}::{node.name}")
    return offenders


@pytest.mark.repo_policy
def test_every_live_jira_test_is_xdist_group_confined() -> None:
    """A test that gates on live `acli` must be pinned to the live-Jira xdist group.

    Without the mark the test can be scheduled onto a different worker than the live
    reconciler E2E group and issue Jira traffic concurrently with it — under `--dist loadgroup`
    too, which is the mode CI's integration tier actually uses.
    """
    offenders = _unconfined_live_jira_tests()
    assert offenders == [], (
        "These tests reach live Jira (they gate on `acli` on PATH) but are NOT confined to the "
        f"`{_LIVE_JIRA_GROUP}` xdist group, so they can run concurrently with the live "
        "reconciler E2E:\n  " + "\n  ".join(offenders) + "\nAdd "
        f'@pytest.mark.xdist_group("{_LIVE_JIRA_GROUP}") (or a module-level pytestmark).'
    )


# --------------------------------------------------------------------------- #
# (a) Behavioural guard — an unsafe distribution mode is rejected at collection
# --------------------------------------------------------------------------- #


def _collect(tmp_path: Path, *dist_args: str, live_env: bool) -> subprocess.CompletedProcess[str]:
    """Collect the live-Jira module in a subprocess. ``--collect-only`` runs no test body."""
    # subprocess_env(), not dict(os.environ): pytest prints call arguments in its default
    # long traceback, so a plain dict would dump every inherited value — including the
    # real JIRA_* credentials this host carries — if subprocess startup ever failed.
    env = subprocess_env()
    for key in ("JIRA_URL", "JIRA_USER", "JIRA_API_TOKEN", "JIRA_PROJECT"):
        env.pop(key, None)
    if live_env:
        # A stub `acli` that is never invoked: --collect-only executes no test.
        stub_dir = tmp_path / "stub-bin"
        stub_dir.mkdir(exist_ok=True)
        stub = stub_dir / "acli"
        stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        stub.chmod(0o755)
        env["PATH"] = f"{stub_dir}{os.pathsep}{env.get('PATH', '')}"
        env["JIRA_URL"] = "https://live-jira.invalid"
        env["JIRA_USER"] = "guard-probe"
        env["JIRA_API_TOKEN"] = "guard-probe"
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            _LIVE_MODULE,
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
            *dist_args,
        ],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


def test_parallel_without_loadgroup_is_rejected_when_live_jira_env_is_present(
    tmp_path: Path,
) -> None:
    """`-n>0` with a group-blind scheduler + live creds must fail collection, not run silently.

    This is the whole defect: under `--dist worksteal` the group mark is inert, so the live-Jira
    tests scatter across workers with no isolation and nothing says so.
    """
    cp = _collect(tmp_path, "-n", "2", "--dist", "worksteal", live_env=True)
    assert cp.returncode != 0, (
        "Collecting group-confined live-Jira tests under `-n 2 --dist worksteal` with live "
        "credentials present was ACCEPTED. The xdist_group mark is ignored by every scheduler "
        f"except loadgroup, so this run has no live-Jira isolation.\n{cp.stdout}\n{cp.stderr}"
    )
    assert _LIVE_JIRA_GROUP in (cp.stdout + cp.stderr)
    assert "loadgroup" in (cp.stdout + cp.stderr)


def test_loadgroup_is_accepted_when_live_jira_env_is_present(tmp_path: Path) -> None:
    """Negative control: `--dist loadgroup` DOES honour the group, so it must pass through."""
    cp = _collect(tmp_path, "-n", "2", "--dist", "loadgroup", live_env=True)
    assert cp.returncode == 0, f"loadgroup was wrongly rejected\n{cp.stdout}\n{cp.stderr}"


def test_serial_run_is_accepted_when_live_jira_env_is_present(tmp_path: Path) -> None:
    """Negative control: with no `-n` there are no workers to scatter across."""
    cp = _collect(tmp_path, live_env=True)
    assert cp.returncode == 0, f"a serial run was wrongly rejected\n{cp.stdout}\n{cp.stderr}"


def test_guard_stays_silent_without_live_jira_credentials(tmp_path: Path) -> None:
    """Negative control: with no creds the tests self-skip, so an unsafe mode is harmless.

    CI runs exactly here — the guard must not redden a build that can issue no Jira traffic.
    """
    cp = _collect(tmp_path, "-n", "2", "--dist", "worksteal", live_env=False)
    assert cp.returncode == 0, f"the guard fired without live credentials\n{cp.stdout}\n{cp.stderr}"
