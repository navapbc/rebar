"""The Burr tripwire (WS-C2): the executor + interpreter must stay a THIN
synchronous pass.

Scoped to ``executor.py`` AND ``interpreter.py`` (the v2 worklist interpreter the
executor delegates to): neither may import a scheduler/concurrency/retry library. If
a future change reaches for asyncio, threads, processes, or a retry lib, this fails —
forcing the deliberate decision (adopt Burr per the trigger list, or stay thin)
rather than letting the engine silently grow one. The trigger-list comment is also
asserted present so the adoption criteria travel with the code.
"""

from __future__ import annotations

import ast
from pathlib import Path

import rebar.llm.workflow.executor as _executor
import rebar.llm.workflow.interpreter as _interpreter

_BANNED = {
    "asyncio",
    "concurrent",
    "concurrent.futures",
    "threading",
    "multiprocessing",
    "tenacity",
    "backoff",
    "retrying",
    "retry",
}


def _executor_source() -> str:
    return Path(_executor.__file__).read_text(encoding="utf-8") + Path(
        _interpreter.__file__
    ).read_text(encoding="utf-8")


def _imported_modules(src: str) -> set[str]:
    tree = ast.parse(src)
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mods.add(alias.name.split(".")[0])
                mods.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                mods.add(node.module.split(".")[0])
                mods.add(node.module)
    return mods


def test_executor_imports_no_scheduler_or_retry_lib() -> None:
    mods = _imported_modules(_executor_source())
    offenders = mods & _BANNED
    assert not offenders, (
        f"executor.py must stay a thin linear pass; banned import(s) found: "
        f"{sorted(offenders)}. Adopt Burr (see the trigger list) before adding "
        f"concurrency/retry, don't grow a scheduler here."
    )


def test_executor_carries_burr_adoption_trigger_list() -> None:
    src = _executor_source().lower()
    assert "burr-adoption trigger list" in src
    assert "tripwire" in src


def test_executor_has_no_threads_at_runtime() -> None:
    # Belt-and-suspenders: even a dynamically-imported scheduler would show here.
    src = _executor_source()
    for needle in ("import asyncio", "import threading", "ThreadPool", "ProcessPool"):
        assert needle not in src, f"executor.py contains {needle!r}"
