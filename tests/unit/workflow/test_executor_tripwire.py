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


def _wf(steps):
    return {"schema_version": "2", "name": "tripwire", "steps": steps}


def test_executor_observes_raising_effectful_step_once_and_stops_downstream() -> None:
    calls: list[str] = []

    def fail_once(ctx):
        calls.append(f"fail:{ctx.step_id}")
        raise RuntimeError("boom")

    def should_not_run(ctx):
        calls.append(f"after:{ctx.step_id}")
        return {"ok": True}

    res = _executor.run_workflow(
        _wf(
            [
                {"id": "fail", "uses": "fail_once"},
                {"id": "after", "uses": "should_not_run", "needs": ["fail"]},
            ]
        ),
        run_id="r",
        scripted_registry={"fail_once": fail_once, "should_not_run": should_not_run},
    )

    assert res.status == "failed"
    assert res.error is not None
    assert "boom" in res.error
    assert calls == ["fail:fail"]
    assert "after" not in res.outputs
    assert "after" not in res.steps


def test_executor_runs_successful_effectful_steps_once_each_in_declared_order() -> None:
    calls: list[str] = []

    def first(ctx):
        calls.append(f"first:{ctx.step_id}")
        return {"value": "ok"}

    def second(ctx):
        calls.append(f"second:{ctx.step_id}")
        return {"seen": ctx.inputs["value"]}

    res = _executor.run_workflow(
        _wf(
            [
                {"id": "first", "uses": "first"},
                {
                    "id": "second",
                    "uses": "second",
                    "needs": ["first"],
                    "with": {"value": "${{ steps.first.outputs.value }}"},
                },
            ]
        ),
        run_id="r",
        scripted_registry={"first": first, "second": second},
    )

    assert res.status == "succeeded"
    assert calls == ["first:first", "second:second"]
    assert res.outputs["second"]["seen"] == "ok"
    assert res.terminal_step == "second"
    assert res.terminal_output == {"seen": "ok"}


def test_executor_never_retries_a_failing_effectful_step() -> None:
    attempts = 0

    def fail_once(ctx):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("boom")

    res = _executor.run_workflow(
        _wf([{"id": "fail", "uses": "fail_once"}]),
        run_id="r",
        scripted_registry={"fail_once": fail_once},
    )

    assert res.status == "failed"
    assert attempts == 1
