# mechanism-ok: test_helper tests/_child_hooks.py — replaces the production env reads
# REBAR_HLC_NOW / REBAR_TEST_COMPACT_RENAME_BARRIER with test-owned child injection
# (story b939-bc68-1cb2-4ee9)
"""Inject rebar's clock and compaction failpoint into a CHILD process.

``src/rebar`` carries no test-only environment read. The two seams a test needs to
drive are plain module-level callables — ``rebar._store.hlc._clock`` and
``rebar._commands.compact_txn._rename_barrier`` — which an in-process test rebinds
with ``monkeypatch.setattr``.

A test that spawns a real ``rebar`` child cannot monkeypatch across the process
boundary, so :func:`child_env` prepends ``_child_hooks_shim/`` to the child's
``PYTHONPATH``. The shim's ``sitecustomize`` module runs at child-interpreter start
and performs the same rebind from the environment values set here. Both child entry
points honour it: ``python -m rebar.cli`` and the ``rebar`` console script each run
``site`` at startup.

The environment names below are the contract between this module (the writer) and
``_child_hooks_shim/sitecustomize.py`` (the reader). They cannot be imported from the
shim, because importing it in the parent would rebind the PARENT's seams, so
``tests/unit/test_child_hooks.py`` asserts the two agree.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from _subprocess_env import SubprocessEnv

#: The shim package whose ``sitecustomize`` the child interpreter imports at startup.
SHIM_DIR = Path(__file__).resolve().parent / "_child_hooks_shim"

#: Pins the child's physical HLC clock to this nanosecond value.
HLC_NOW_ENV = "REBAR_TEST_HLC_NOW"

#: Names a directory the child's compaction rename barrier pauses in.
RENAME_BARRIER_ENV = "REBAR_TEST_COMPACT_RENAME_BARRIER"


def child_env(
    env: Mapping[str, str],
    *,
    hlc_now: int | str | None = None,
    rename_barrier: str | Path | None = None,
) -> SubprocessEnv:
    """Return *env* with the requested child hooks installed.

    Requesting no hook returns an unperturbed copy — the shim is only put on the
    child's path when something actually needs rebinding, so an ordinary child runs
    exactly as it did before.
    """
    result = SubprocessEnv(env)
    if hlc_now is None and rename_barrier is None:
        return result
    if hlc_now is not None:
        result[HLC_NOW_ENV] = str(hlc_now)
    if rename_barrier is not None:
        result[RENAME_BARRIER_ENV] = str(rename_barrier)
    result["PYTHONPATH"] = _with_shim_first(result.get("PYTHONPATH", ""))
    return result


def _with_shim_first(pythonpath: str) -> str:
    """``pythonpath`` with :data:`SHIM_DIR` first and never duplicated."""
    shim = str(SHIM_DIR)
    rest = [p for p in pythonpath.split(os.pathsep) if p and p != shim]
    return os.pathsep.join([shim, *rest])
