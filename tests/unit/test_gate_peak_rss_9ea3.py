"""Report gate peak RSS at completion (9ea3).

An MCP server was OOM-killed during in-process review, with no per-run
memory measure. Normalize ru_maxrss, which reports bytes on macOS/BSD and KiB on Linux.
Emit even when the gate raises, using one line-start GATE_PEAK_RSS JSON marker
on stderr and a logger copy without the token. Measurement must not raise
into the gate."""

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
    """The logger copy carries only JSON, not the marker token (f829).
    Otherwise journald sees two line-start GATE_PEAK_RSS records per run."""
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


class _Handle:
    """The gate handle both entry points thread through; only ``source`` is read here."""

    source = "attested"


@pytest.fixture
def _stubbed_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neuter everything the two entry points do EXCEPT the measurement.

    The gate body resolves a read-root, binds an LLM config and runs three LLM passes. None
    of that is what is under test here and all of it is slow, networked or both, so each
    seam is replaced with a trivial stand-in. What is deliberately NOT stubbed is
    ``peak_rss``: the marker has to come out of the real emitter, through the real entry
    point, or the test proves nothing about either.
    """
    import contextlib

    from rebar.llm import completion, config_binding, gate_source
    from rebar.llm import plan_review as plan_review_pkg

    monkeypatch.setattr(gate_source, "resolve_gate_handle", lambda *a, **k: _Handle())
    monkeypatch.setattr(gate_source, "gate_read_root", lambda _h: contextlib.nullcontext())
    monkeypatch.setattr(gate_source, "apply_handle", lambda bound, _h: bound)
    monkeypatch.setattr(gate_source, "annotate_result", lambda verdict, _h: verdict)
    monkeypatch.setattr(
        config_binding,
        "compose_and_bind_llm_config",
        lambda **_k: contextlib.nullcontext(object()),
    )
    monkeypatch.setattr(plan_review_pkg, "_run_plan_review", lambda _t, **_k: {"verdict": "PASS"})
    monkeypatch.setattr(
        completion, "_run_completion_at_handle", lambda _t, **_k: {"verdict": "PASS"}
    )
    monkeypatch.setattr(
        completion,
        "_resolve_completion_ticket_view",
        lambda handle, **_k: (handle, None, "eager"),
    )


@pytest.mark.parametrize(
    ("entry_point", "gate"),
    [("review_plan", "plan_review"), ("verify_completion", "verify_completion")],
)
def test_both_gate_entry_points_emit_the_marker(
    _stubbed_gate: None,
    capsys: pytest.CaptureFixture[str],
    entry_point: str,
    gate: str,
) -> None:
    """Call both library gate entry points and observe a peak-RSS marker.

    MCP and CLI reach review_plan and verify_completion through these functions.
    A source grep could pass for dead or mislabeled instrumentation. Execution
    checks the emitted marker on both paths."""
    import rebar.llm as llm

    verdict = getattr(llm, entry_point)("9ea3-7d07-ea55-4496")

    assert verdict == {"verdict": "PASS"}, "the stubbed gate result must pass through intact"
    records = _marker_records(capsys.readouterr().err)
    assert len(records) == 1, f"{entry_point} emitted {len(records)} markers, expected exactly 1"
    assert records[0]["gate"] == gate
    assert records[0]["ticket_id"] == "9ea3-7d07-ea55-4496"
    assert isinstance(records[0]["peak_rss_bytes"], int)
