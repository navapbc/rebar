"""The parent/child contract in ``tests/_child_hooks.py`` and its shim.

``child_env`` writes environment variables that only the child's ``sitecustomize``
reads, so the two halves agree by convention rather than by a shared import — the
shim cannot be imported in the parent without rebinding the parent's own seams.
Drift between them is silent: the child simply never installs the hook and the
waiting test hangs to its deadline. These cases pin the contract and the blast
radius of putting the shim on a child's path.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import ModuleType

import pytest
from _child_hooks import HLC_NOW_ENV, RENAME_BARRIER_ENV, SHIM_DIR, child_env


def _load_shim(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Import the shim under a private name with both hooks unset, so it stays inert."""
    monkeypatch.delenv(HLC_NOW_ENV, raising=False)
    monkeypatch.delenv(RENAME_BARRIER_ENV, raising=False)
    spec = importlib.util.spec_from_file_location(
        "_child_hooks_shim_under_test", SHIM_DIR / "sitecustomize.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_shim_reads_the_names_the_helper_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    shim = _load_shim(monkeypatch)
    assert (shim.HLC_NOW_ENV, shim.RENAME_BARRIER_ENV) == (HLC_NOW_ENV, RENAME_BARRIER_ENV)


def test_requesting_no_hook_leaves_the_child_path_alone() -> None:
    before = {"PATH": "/usr/bin", "PYTHONPATH": "/somewhere"}
    assert dict(child_env(before)) == before


def test_the_shim_goes_first_without_dropping_an_existing_pythonpath() -> None:
    env = child_env({"PYTHONPATH": "/somewhere"}, hlc_now=7)
    assert env["PYTHONPATH"].split(os.pathsep) == [str(SHIM_DIR), "/somewhere"]
    assert env[HLC_NOW_ENV] == "7"


def test_a_second_call_does_not_stack_the_shim_entry() -> None:
    once = child_env({}, hlc_now=7)
    twice = child_env(once, rename_barrier=Path("/tmp/barrier"))
    assert twice["PYTHONPATH"].split(os.pathsep) == [str(SHIM_DIR)]
    assert twice[RENAME_BARRIER_ENV] == "/tmp/barrier"
