"""A finished background job must not leave its freed memory held by the MCP process.

Bug unmedical-stational-chrysalis: after plan-review and completion gates finished, the
on-box MCP container sat idle at 1.2-2.3 GB for hours. The gates' large strings are C-heap
allocations on their worker threads; glibc keeps the freed pages in that thread's arena
until ``malloc_trim`` hands them back. The in-flight gauge now does that when its count
returns to zero.
"""

from __future__ import annotations

import ctypes
import random
import sys
import threading

import pytest

from rebar import _mcp_health
from rebar._mcp_health import InFlightGauge, release_freed_heap

pytestmark = pytest.mark.unit


def _count_trims(monkeypatch) -> list[int]:
    calls: list[int] = []
    monkeypatch.setattr(_mcp_health, "release_freed_heap", lambda: calls.append(1) or True)
    return calls


def test_last_release_returns_freed_heap(monkeypatch):
    calls = _count_trims(monkeypatch)
    gauge = InFlightGauge()
    first = gauge.hold()
    second = gauge.admit("review_plan")

    first()
    assert calls == [], "work is still in flight; trimming now would race a running gate"
    second()
    assert calls == [1]
    second()
    assert calls == [1], "a repeated release must not trim again"


def test_tracked_certified_call_trims_when_it_finishes(monkeypatch):
    calls = _count_trims(monkeypatch)
    gauge = InFlightGauge()
    with gauge.track("verify_completion"):
        assert calls == []
    assert calls == [1]
    with gauge.track("show_ticket"):
        pass
    assert calls == [1], "uncounted tools never move the gauge"


def test_release_freed_heap_reports_availability():
    assert release_freed_heap() is sys.platform.startswith("linux")


def test_windows_has_no_trim_and_a_release_does_not_raise(monkeypatch):
    """On Windows ``ctypes.CDLL(None)`` raises TypeError; every certified tool call failed
    there because its gauge release reached the trim (bug equatorial-iconic-cardinal)."""
    real_cdll = ctypes.CDLL

    class _WindowsCDLL(real_cdll):
        def __init__(self, name, *args, **kwargs):
            if name is None:
                raise TypeError("LoadLibrary() argument 1 must be str, not None")
            super().__init__(name, *args, **kwargs)

    monkeypatch.setattr(ctypes, "CDLL", _WindowsCDLL)
    monkeypatch.setattr(sys, "platform", "win32")
    _mcp_health._malloc_trim.cache_clear()
    try:
        assert release_freed_heap() is False
        gauge = InFlightGauge()
        gauge.hold()()
        with gauge.track("transition_ticket"):
            pass
        assert gauge.value == 0
    finally:
        _mcp_health._malloc_trim.cache_clear()


def _rss_anon_mib() -> int:
    with open("/proc/self/status", encoding="ascii") as status:
        for line in status:
            if line.startswith("RssAnon:"):
                return int(line.split()[1]) // 1024
    raise AssertionError("RssAnon missing from /proc/self/status")


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="glibc arena behaviour")
def test_finished_background_job_does_not_keep_its_memory():
    """Shape of a gate: a counted worker thread builds and drops large strings while a
    few small results outlive it, which pins the freed pages inside its arena."""
    gauge = InFlightGauge()
    survivors: list[bytes] = []
    held: dict[str, int] = {}

    def _gate_like_job() -> None:
        rng = random.Random(0)
        bulk = []
        for i in range(12000):
            bulk.append("x" * rng.randint(4096, 40000))
            if i % 50 == 0:
                survivors.append(bytes(rng.randint(600, 4000)))
        del bulk
        held["after_free"] = _rss_anon_mib()

    baseline = _rss_anon_mib()
    release = gauge.hold()
    worker = threading.Thread(target=_gate_like_job)
    worker.start()
    worker.join()
    assert held["after_free"] - baseline > 128, (
        "precondition: glibc should still hold the freed pages before the gauge goes idle; "
        f"baseline {baseline} MiB, after free {held['after_free']} MiB"
    )

    release()

    idle = _rss_anon_mib()
    assert idle - baseline < 64, (
        f"idle process kept {idle - baseline} MiB after its last job finished "
        f"(baseline {baseline} MiB, before release {held['after_free']} MiB)"
    )
    assert len(survivors) == 240
