"""Held-out regression oracle for the grounding worker spawn contract."""

from __future__ import annotations

import multiprocessing
import pickle
import threading
import warnings

import pytest

from rebar.grounding import harness

from . import _worker_payloads as wp

pytestmark = pytest.mark.unit


def test_default_worker_uses_spawn_while_another_thread_is_live() -> None:
    stop = threading.Event()
    thread = threading.Thread(target=stop.wait, daemon=True)
    thread.start()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            result = harness.run_in_worker(wp.process_class_name, backend="test")
    finally:
        stop.set()
        thread.join(timeout=1)

    assert result.completed
    assert result.value == "SpawnProcess"


def test_explicit_spawn_context_is_supported() -> None:
    result = harness.run_in_worker(
        wp.process_class_name,
        backend="test",
        mp_context=multiprocessing.get_context("spawn"),
    )

    assert result.completed
    assert result.value == "SpawnProcess"


def test_unpickleable_callable_fails_open_at_spawn_boundary() -> None:
    result = harness.run_in_worker(lambda: "unreachable", backend="test")

    assert result.abstained
    assert result.abstain_reason == "other"
    assert "spawn failed" in (result.detail or "")


def test_receive_unpickle_failure_fails_open() -> None:
    class _ParentConnection:
        def poll(self, timeout: float) -> bool:
            return True

        def recv(self) -> object:
            raise pickle.UnpicklingError("corrupt worker result")

        def close(self) -> None:
            pass

    class _ChildConnection:
        def close(self) -> None:
            pass

    class _Process:
        exitcode = 0
        pid = 1

        def start(self) -> None:
            pass

        def terminate(self) -> None:
            pass

        def kill(self) -> None:
            pass

        def join(self, timeout: float) -> None:
            pass

        def is_alive(self) -> bool:
            return False

        def close(self) -> None:
            pass

    class _Context:
        def Pipe(self, *, duplex: bool) -> tuple[_ParentConnection, _ChildConnection]:
            return _ParentConnection(), _ChildConnection()

        def Process(self, *, target: object, args: object) -> _Process:
            return _Process()

    result = harness.run_in_worker(
        wp.process_class_name,
        backend="test",
        mp_context=_Context(),  # type: ignore[arg-type]
    )

    assert result.abstained
    assert result.abstain_reason == "other"
    assert "receive failed" in (result.detail or "")
