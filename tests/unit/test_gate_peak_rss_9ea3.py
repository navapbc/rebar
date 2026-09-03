"""A gate run reports its peak RSS when it completes (bug 9ea3-7d07-ea55-4496).

MEASUREMENT ONLY. The mcp container was OOM-killed roughly three minutes into a
plan-review run, on a box where gate work executes IN-PROCESS on an MCP daemon thread —
so gate memory is server memory, and nobody had measured what one run costs. Every
candidate remedy needs that number, so :mod:`rebar.llm.peak_rss` produces it and does
nothing else.

These tests pin the three things that make the number trustworthy:

* ``ru_maxrss`` UNITS DIFFER BY PLATFORM — bytes on macOS/BSD, kibibytes on Linux (the
  box). The error is a silent factor of 1024 in either direction and either way lands on
  a plausible-looking figure, so both branches are pinned;
* the marker is emitted AT COMPLETION, including when the gate RAISES — which is the
  OOM-adjacent case this exists for;
* it is emitted in the established journald convention (ONE line-start ``GATE_PEAK_RSS
  {json}`` on stderr, a logger copy of the body WITHOUT the token) and never raises into
  the run it measures.
"""

from __future__ import annotations

import json

import pytest

from rebar.llm import peak_rss


def _marker_records(captured: str) -> list[dict]:
    return [
        json.loads(line[len(peak_rss.MARKER) + 1 :])
        for line in captured.splitlines()
        if line.startswith(peak_rss.MARKER + " ")
    ]


@pytest.mark.parametrize(
    ("platform", "expected"),
    [
        # Linux (and the rebar box) reports KIBIBYTES.
        ("linux", 2048 * 1024),
        # macOS and the BSDs report BYTES.
        ("darwin", 2048),
        ("freebsd14", 2048),
        ("openbsd7", 2048),
        ("netbsd10", 2048),
    ],
)
def test_ru_maxrss_units_are_converted_per_platform(platform: str, expected: int) -> None:
    assert peak_rss.ru_maxrss_to_bytes(2048, platform) == expected


def test_the_two_platform_families_disagree_by_exactly_1024() -> None:
    """ANTI-VACUITY: a conversion that ignored the platform would pass the table above on
    whichever branch it happened to implement. The two families must differ, by the exact
    factor that makes the mistake survivable-looking (a 2 GiB run read as 2 MiB, or a
    2 MiB run read as 2 GiB)."""
    assert peak_rss.ru_maxrss_to_bytes(4096, "linux") == 1024 * peak_rss.ru_maxrss_to_bytes(
        4096, "darwin"
    )


def test_peak_rss_bytes_reads_the_live_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """The live read routes through the same conversion, keyed on the running platform."""
    import resource

    class _Usage:
        ru_maxrss = 5000

    monkeypatch.setattr(resource, "getrusage", lambda _who: _Usage())
    monkeypatch.setattr(peak_rss.sys, "platform", "linux")
    assert peak_rss.peak_rss_bytes() == 5000 * 1024

    monkeypatch.setattr(peak_rss.sys, "platform", "darwin")
    assert peak_rss.peak_rss_bytes() == 5000


def test_completion_emits_one_line_start_marker(capsys: pytest.CaptureFixture[str]) -> None:
    with peak_rss.gate_peak_rss("plan_review", "9ea3-7d07-ea55-4496"):
        pass

    records = _marker_records(capsys.readouterr().err)
    assert len(records) == 1
    record = records[0]
    assert record["event"] == peak_rss.MARKER
    assert record["gate"] == "plan_review"
    assert record["ticket_id"] == "9ea3-7d07-ea55-4496"
    assert isinstance(record["peak_rss_bytes"], int)
    assert record["peak_rss_bytes"] > 0
    assert isinstance(record["peak_delta_bytes"], int)
    assert isinstance(record["elapsed_ms"], int)


def test_the_logger_copy_carries_no_line_start_token(
    caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    """Bug f829-152a-b415-44a4: the logger copy logs the JSON body only.

    If it emitted the token too, configured application logging (whose stdout also lands
    in journald) would DOUBLE any future ``^GATE_PEAK_RSS \\{`` count — the same defect
    the voter markers were fixed for."""
    with caplog.at_level("INFO", logger="rebar.llm.peak_rss"):
        with peak_rss.gate_peak_rss("verify_completion", "9ea3-7d07-ea55-4496"):
            pass
    capsys.readouterr()

    assert caplog.messages, "the logger copy was not emitted at all"
    for message in caplog.messages:
        assert not message.startswith(peak_rss.MARKER)
        assert json.loads(message)["event"] == peak_rss.MARKER


def test_a_raising_gate_still_reports_its_peak(capsys: pytest.CaptureFixture[str]) -> None:
    """The measurement is emitted from a ``finally``.

    A gate that dies is precisely the case this instrumentation was built for: the run
    that OOM-killed the container never returned a verdict, and a marker emitted only on
    the success path would have measured everything except the run that mattered. The
    exception must still propagate unchanged."""
    with pytest.raises(RuntimeError, match="gate blew up"):
        with peak_rss.gate_peak_rss("plan_review", "9ea3-7d07-ea55-4496"):
            raise RuntimeError("gate blew up")

    records = _marker_records(capsys.readouterr().err)
    assert len(records) == 1
    assert records[0]["gate"] == "plan_review"


def test_measurement_failure_never_breaks_the_gate(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Instrumentation must never be able to fail the run it is measuring."""

    def _boom(_who: object) -> object:
        raise OSError("rusage unavailable")

    import resource

    monkeypatch.setattr(resource, "getrusage", _boom)

    with pytest.raises(OSError, match="rusage unavailable"):
        peak_rss.peak_rss_bytes()

    # …but the emitter itself swallows whatever it is handed.
    monkeypatch.setattr(peak_rss, "peak_rss_bytes", _boom)
    peak_rss.emit_gate_peak_rss("plan_review", "t", {"peak_rss_bytes": object()})

    assert _marker_records(capsys.readouterr().err) == []


def test_both_gate_entry_points_wrap_themselves_in_the_measurement() -> None:
    """The seam is the LIBRARY entry points, not the MCP daemon and the CLI separately.

    ``_mcp_llm._spawn_gate_daemon`` and the CLI verbs both reach the gate through
    ``rebar.llm.review_plan`` / ``rebar.llm.verify_completion``, so wrapping those two
    covers both call paths once. Pinning it here keeps a future refactor from dropping
    the instrumentation out of one path silently."""
    import inspect

    from rebar.llm.completion import verify_completion
    from rebar.llm.plan_review import review_plan

    for func, gate in ((review_plan, "plan_review"), (verify_completion, "verify_completion")):
        source = inspect.getsource(func)
        assert f'gate_peak_rss("{gate}"' in source, f"{func.__name__} does not measure peak RSS"
