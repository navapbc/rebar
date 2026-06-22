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


def test_executor_documents_the_burr_adoption_path() -> None:
    # The adoption mechanism should travel with the code so a future maintainer knows
    # WHEN to adopt Burr instead of growing a scheduler. We assert the CONCEPT is
    # documented (tolerant of rewording — not an exact-phrase pin): "burr" appears and
    # at least the four numbered trigger criteria are present.
    src = _executor_source().lower()
    assert "burr" in src
    assert sum(f"{n}." in src for n in (1, 2, 3, 4)) >= 4  # the trigger list's items survive


def _imports_a_banned_concurrency_lib(module) -> bool:
    mods = _imported_modules(Path(module.__file__).read_text(encoding="utf-8"))
    return bool(mods & {"threading", "concurrent", "concurrent.futures", "multiprocessing"})


def test_map_fanout_is_the_sole_concurrency_module() -> None:
    # STRUCTURAL (AST), not prose-grep: bounded-concurrent map fan-out is the ONE narrow
    # relaxation (8d8e). map_fanout.py is the only workflow module that may import a
    # concurrency lib; the tripwire-scanned executor + interpreter must NOT. (The
    # behavioral guarantee — commits stay serialized — is proven separately in
    # test_map_fanout.py::test_commits_are_serialized_even_under_concurrency.)
    import rebar.llm.workflow.executor as _exe
    import rebar.llm.workflow.interpreter as _interp
    import rebar.llm.workflow.map_fanout as _fanout

    assert _imports_a_banned_concurrency_lib(_fanout)  # the relaxation really lives here
    assert not _imports_a_banned_concurrency_lib(_exe)
    assert not _imports_a_banned_concurrency_lib(_interp)
    # And the deliberate exception is documented (concept present, not an exact phrase).
    assert "rationale" in Path(_fanout.__file__).read_text(encoding="utf-8").lower()
