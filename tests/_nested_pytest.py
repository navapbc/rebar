"""One place where the test suite launches a nested pytest.

Every nested run built by hand re-decided basetemp, timeout, cache plugin and env, and the
``--basetemp`` omission that lets a child clean another run's shared numbered temp root arrived
as three separate bugs before this module existed.  Route every launch through
:func:`run_nested_pytest` so ``--basetemp`` cannot be forgotten again.

Two launches under ``tests/`` deliberately stay outside this helper, because routing them
through ``python -m pytest`` would put the repository root on the child's ``sys.path`` and
destroy the very thing they reproduce: ``tests/unit/test_scripts_import_convention.py`` and
``tests/unit/test_tests_import_convention.py`` spawn the BARE ``pytest`` console script from a
cwd outside the repository.  The ``pytester.runpytest*`` calls in
``tests/unit/test_caplog_coverage_integrity.py`` and ``tests/unit/test_repo_isolation_guard.py``
also stay as they are: pytest owns their basetemp already.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The pytest option this module exists to make unforgettable.
BASETEMP_FLAG = "--basetemp"

#: Directory name the basetemp option always points at, under the caller's tmp_path.
NESTED_BASETEMP_NAME = "nested-pytest"


def nested_basetemp(tmp_path: Path) -> Path:
    """The basetemp :func:`run_nested_pytest` will hand a child rooted at *tmp_path*."""
    return Path(tmp_path) / NESTED_BASETEMP_NAME


def run_nested_pytest(
    tmp_path: Path,
    *args: str,
    env: Mapping[str, str],
    timeout: float | None = None,
    cwd: Path | None = REPO_ROOT,
    no_cacheprovider: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run ``python -m pytest`` in a child whose basetemp is owned by *tmp_path*.

    *env* is REQUIRED and is forwarded to the child verbatim — it is never merged onto
    ``os.environ``, re-wrapped, or given or denied a key.  Several callers hand the child a
    deliberately minimal mapping so pytest's bounded mapping repr cannot truncate a sentinel
    away; widening it would leave those oracles green and vacuous.  A caller that wants the
    ambient environment asks for it explicitly with ``subprocess_env()``.

    *tmp_path* need not exist yet: pytest's ``--basetemp`` does NOT create missing parent
    directories and fails every child test with ``FileNotFoundError`` when they are absent, so
    the directory is created here rather than at each call site.

    *timeout* reaches :func:`subprocess.run` as a keyword and a :class:`subprocess.TimeoutExpired`
    propagates to the caller, so a caller can convert it into its own diagnostic.  No ``check``
    is applied and the :class:`subprocess.CompletedProcess` is returned unmodified.
    """
    Path(tmp_path).mkdir(parents=True, exist_ok=True)
    command = [sys.executable, "-m", "pytest"]
    if no_cacheprovider:
        command += ["-p", "no:cacheprovider"]
    command += [*args, BASETEMP_FLAG, str(nested_basetemp(tmp_path))]
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
