"""Launch nested pytest with a test-owned basetemp.

Bare pytest and pytester exceptions preserve their import or temp-root contracts and
supply basetemp themselves. The uniqueness guard checks all launch spellings, including
python -c payloads, so children cannot clean another run's shared temp root.
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
    """Run pytest with basetemp under tmp_path and return its unmodified result.

    The caller supplies env verbatim. Minimal mappings must not gain ambient keys. Create
    the parent directory because pytest will not. TimeoutExpired propagates and no check
    is applied to the child exit.
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
        check=False,
    )
