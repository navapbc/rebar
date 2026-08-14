"""Behavioral tests for thresholded CI fixture and nested-span timing."""

from __future__ import annotations

import re
import warnings
from pathlib import Path
from types import SimpleNamespace

import pytest


def _request(nodeid: str = "tests/unit/test_sample.py::test_case") -> SimpleNamespace:
    return SimpleNamespace(node=SimpleNamespace(nodeid=nodeid))


@pytest.fixture
def root_conftest(pytestconfig):
    expected = Path(__file__).resolve().parents[1] / "conftest.py"
    for plugin in pytestconfig.pluginmanager.get_plugins():
        plugin_file = getattr(plugin, "__file__", None)
        if plugin_file is not None and Path(plugin_file).resolve() == expected:
            return plugin
    pytest.fail(f"root pytest plugin was not loaded from {expected}")


def _run_fixture_hook(root_conftest, fixture_name: str = "slow_fixture") -> None:
    hook = root_conftest.pytest_fixture_setup(
        SimpleNamespace(argname=fixture_name),
        _request(),
    )
    next(hook)
    with pytest.raises(StopIteration):
        next(hook)


def test_fixture_setup_hook_reports_worker_node_fixture_and_elapsed(monkeypatch, root_conftest):
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("REBAR_FIXTURE_TIMING", "1")
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw2")
    readings = iter((10.0, 11.25))
    monkeypatch.setattr(root_conftest.time, "monotonic", lambda: next(readings))

    with pytest.warns(
        pytest.PytestWarning,
        match=(
            r"\[rebar-ci-timing\] worker=gw2 "
            r"node=tests/unit/test_sample.py::test_case fixture=slow_fixture "
            r"span=fixture_setup elapsed=1\.250s"
        ),
    ):
        _run_fixture_hook(root_conftest)


@pytest.mark.parametrize(
    ("armed", "elapsed"),
    [
        (False, 2.0),
        (True, 0.999),
    ],
)
def test_fixture_setup_hook_is_silent_when_unarmed_or_below_threshold(
    monkeypatch, root_conftest, armed, elapsed
):
    monkeypatch.delenv("CI", raising=False)
    if armed:
        monkeypatch.setenv("REBAR_FIXTURE_TIMING", "1")
    else:
        monkeypatch.delenv("REBAR_FIXTURE_TIMING", raising=False)
    readings = iter((20.0, 20.0 + elapsed))
    monkeypatch.setattr(root_conftest.time, "monotonic", lambda: next(readings))

    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        _run_fixture_hook(root_conftest)

    assert recorded == []


@pytest.mark.parametrize(
    "span_name",
    [
        "threading_http_server_constructor",
        "tcp_server_bind",
        "socket.getfqdn",
    ],
)
def test_timed_call_attributes_each_editor_span_and_preserves_result(
    monkeypatch, root_conftest, span_name
):
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("REBAR_FIXTURE_TIMING", "1")
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    readings = iter((30.0, 31.5))
    monkeypatch.setattr(root_conftest.time, "monotonic", lambda: next(readings))
    sentinel = object()
    received = []
    escaped_span = re.escape(span_name)

    def target(*args, **kwargs):
        received.append((args, kwargs))
        return sentinel

    with pytest.warns(
        pytest.PytestWarning,
        match=(
            rf"\[rebar-ci-timing\] worker=main .* fixture=_server "
            rf"span={escaped_span} elapsed=1\.500s"
        ),
    ):
        result = root_conftest._timed_ci_call(
            _request().node.nodeid,
            "_server",
            span_name,
            target,
            "arg",
            keyword="value",
        )

    assert result is sentinel
    assert received == [(("arg",), {"keyword": "value"})]


def test_timed_call_preserves_original_exception(monkeypatch, root_conftest):
    monkeypatch.setenv("CI", "true")
    monkeypatch.delenv("REBAR_FIXTURE_TIMING", raising=False)
    readings = iter((40.0, 42.0))
    monkeypatch.setattr(root_conftest.time, "monotonic", lambda: next(readings))
    failure = RuntimeError("original failure")

    def target():
        raise failure

    with pytest.warns(pytest.PytestWarning, match=r"span=socket\.getfqdn elapsed=2\.000s"):
        with pytest.raises(RuntimeError) as raised:
            root_conftest._timed_ci_call(
                _request().node.nodeid,
                "_server",
                "socket.getfqdn",
                target,
            )

    assert raised.value is failure
