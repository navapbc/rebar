"""Launch nested pytest with a test-owned basetemp.

Bare pytest and pytester exceptions preserve their import or temp-root contracts and
supply basetemp themselves. The uniqueness guard checks all launch spellings, including
python -c payloads, so children cannot clean another run's shared temp root.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The pytest option this module exists to make unforgettable.
BASETEMP_FLAG = "--basetemp"

#: Directory name the basetemp option always points at, under the caller's tmp_path.
NESTED_BASETEMP_NAME = "nested-pytest"


def nested_basetemp(tmp_path: Path) -> Path:
    """The basetemp :func:`run_nested_pytest` will hand a child rooted at *tmp_path*."""
    return Path(tmp_path) / NESTED_BASETEMP_NAME


#: Windows will not start Winsock in a process without it, so asyncio (and anyio's pytest11
#: plugin, which imports asyncio) fails to load in a child given a minimal mapping (bug 21fb).
_WINDOWS_REQUIRED_KEY = "SYSTEMROOT"


def _child_env(
    env: Mapping[str, str],
    *,
    os_name: str = os.name,
    ambient: Mapping[str, str] = os.environ,
) -> Mapping[str, str]:
    """The mapping to hand the child: *env* itself, plus ``SYSTEMROOT`` on Windows.

    Only on ``nt``, and only when the caller supplied no ``SYSTEMROOT`` (Windows keys are
    case-insensitive) while the ambient environment has one, is a copy returned. The copy keeps
    the caller's mapping type, so a redacting ``SubprocessEnv`` stays redacting, and the
    caller's object is never mutated. Everywhere else the caller's object passes through.
    """
    if os_name != "nt" or any(key.upper() == _WINDOWS_REQUIRED_KEY for key in env):
        return env
    value = next(
        (v for k, v in ambient.items() if k.upper() == _WINDOWS_REQUIRED_KEY),
        None,
    )
    if value is None:
        return env
    child = type(env)(env) if isinstance(env, dict) else dict(env)
    child[_WINDOWS_REQUIRED_KEY] = value
    return child


#: Options with which a caller already decides where the child's rootdir and conftest walk stop.
_ROOTDIR_OPTIONS = ("--rootdir", "--config-file", "--confcutdir")


def _pins_rootdir(arg: str) -> bool:
    if arg.startswith("-c"):  # ``-c FILE`` and ``-cFILE``; pytest has no other ``-c...`` option
        return True
    return any(arg == opt or arg.startswith(f"{opt}=") for opt in _ROOTDIR_OPTIONS)


def _is_within(path: str, base: str, pathmod: Any) -> bool:
    path, base = pathmod.normcase(path), pathmod.normcase(base)
    try:
        return bool(pathmod.commonpath([path, base]) == base)
    except ValueError:  # different drives share no directory
        return False


def _rootdir_pin(
    args: Sequence[str],
    cwd: str | os.PathLike[str] | None,
    *,
    pathmod: Any = os.path,
    exists: Callable[[str], bool] = os.path.exists,
    isdir: Callable[[str], bool] = os.path.isdir,
) -> list[str]:
    """``["--rootdir", <dir>]`` when every path argument lies outside *cwd*, else ``[]``.

    With no ini file, pytest roots the session at the common ancestor of the cwd and the
    arguments' directories, unless that ancestor is a filesystem root, in which case it uses
    the arguments' directory. On Windows a cwd on ``D:`` and a tmp_path test file on ``C:``
    share no ancestor, so pytest keeps the cwd as rootdir and confcutdir. Every ancestor of
    the test file then counts as inside confcutdir, collection scans each of them from
    ``C:\\`` down, and on win32 it lstats every entry of the shared ``%TEMP%``, so a
    directory another process removes fails the child's collection (bug
    refractive-hasty-iguanodon). Pinning the arguments' directory gives every platform the
    rootdir a macOS run already gets.

    Path arguments are picked the way pytest's ``get_dirs_from_args`` picks them: each
    non-option argument, cut at ``::``, that exists on disk; a directory stands for itself
    and a file for its parent. Relative arguments, arguments under *cwd*, and callers that
    pass ``--rootdir``, ``-c``/``--config-file`` or ``--confcutdir`` keep pytest's choice.
    """
    if any(_pins_rootdir(arg) for arg in args):
        return []
    base = os.fspath(cwd) if cwd is not None else os.getcwd()
    dirs: list[str] = []
    for arg in args:
        path = arg.split("::", 1)[0]
        if arg.startswith("-") or not pathmod.isabs(path) or not exists(path):
            continue
        if _is_within(path, base, pathmod):
            return []
        dirs.append(path if isdir(path) else pathmod.dirname(path))
    if not dirs:
        return []
    try:
        return ["--rootdir", pathmod.commonpath(dirs)]
    except ValueError:  # arguments on different drives: no single directory contains them
        return []


def run_nested_pytest(
    tmp_path: Path,
    *args: str,
    env: Mapping[str, str],
    timeout: float | None = None,
    cwd: Path | None = REPO_ROOT,
    no_cacheprovider: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run pytest with basetemp under tmp_path and return its unmodified result.

    The caller supplies env verbatim. Minimal mappings must not gain ambient keys, with one
    exception: on Windows the OS-required ``SYSTEMROOT`` is forwarded (see ``_child_env``). Create
    the parent directory because pytest will not. TimeoutExpired propagates and no check
    is applied to the child exit.
    """
    Path(tmp_path).mkdir(parents=True, exist_ok=True)
    command = [sys.executable, "-m", "pytest"]
    if no_cacheprovider:
        command += ["-p", "no:cacheprovider"]
    command += [*args, *_rootdir_pin(args, cwd), BASETEMP_FLAG, str(nested_basetemp(tmp_path))]
    return subprocess.run(
        command,
        cwd=cwd,
        env=_child_env(env),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
