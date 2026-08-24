"""Focused synthetic tests for the reusable gate-duration probe."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
import rebar_duration_probe as probe

pytestmark = pytest.mark.scripts


def _iso(second: int) -> str:
    return datetime.fromtimestamp(second, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text("".join(json.dumps(record) + "\n" for record in records))


def test_classify_command_separates_gate_operations_and_exclusions() -> None:
    plan = probe.classify_command("rtk proxy rebar review-plan sample-ticket --output json")
    status = probe.classify_command("rtk rebar review-plan sample-ticket --status")
    forced = probe.classify_command(
        "rtk rebar transition sample-ticket in_progress closed --force=x"
    )

    assert plan == [
        {
            "operation": "plan",
            "target": "sample-ticket",
            "args": ["review-plan", "sample-ticket", "--output", "json"],
            "excluded": None,
        }
    ]
    assert status[0]["excluded"] == "status"
    assert forced[0]["operation"] == "close"
    assert forced[0]["excluded"] == "force"


@pytest.mark.parametrize("shell", ["sh", "/bin/zsh"])
def test_classify_command_unwraps_shell_command_strings(shell: str) -> None:
    classified = probe.classify_command(
        f"{shell} -lc 'rtk proxy rebar review-plan sample-ticket --output json'"
    )

    assert classified == [
        {
            "operation": "plan",
            "target": "sample-ticket",
            "args": ["review-plan", "sample-ticket", "--output", "json"],
            "excluded": None,
        }
    ]


def test_parse_logs_links_initial_yield_and_pty_poll(tmp_path: Path) -> None:
    source = (
        "const r = await tools.exec_command({cmd:`rtk rebar transition "
        "sample-ticket in_progress closed`, workdir:`/tmp`}); text(JSON.stringify(r));"
    )
    poll = (
        "const r = await tools.write_stdin({session_id:42,chars:``,yield_time_ms:30000}); "
        "text(JSON.stringify(r));"
    )
    records = [
        {
            "timestamp": _iso(1_000),
            "payload": {
                "type": "custom_tool_call",
                "name": "exec",
                "call_id": "initial",
                "input": source,
            },
        },
        {
            "timestamp": _iso(1_001),
            "payload": {
                "type": "custom_tool_call_output",
                "call_id": "initial",
                "output": json.dumps({"wall_time_seconds": 1, "session_id": 42}),
            },
        },
        {
            "timestamp": _iso(1_002),
            "payload": {
                "type": "custom_tool_call",
                "name": "exec",
                "call_id": "poll",
                "input": poll,
            },
        },
        {
            "timestamp": _iso(1_003),
            "payload": {
                "type": "custom_tool_call_output",
                "call_id": "poll",
                "output": json.dumps({"wall_time_seconds": 3, "exit_code": 0}),
            },
        },
    ]
    _write_jsonl(tmp_path / "rollout.jsonl", records)

    invocations, audit = probe.parse_logs([tmp_path])

    assert audit["log_files"] == 1
    assert len(invocations) == 1
    invocation = invocations[0]
    assert invocation.operation == "close"
    assert invocation.target == "sample-ticket"
    assert invocation.session_id == 42
    assert invocation.exit_code == 0
    assert invocation.duration == pytest.approx(3)


def test_parse_logs_audits_unknown_yielded_cell(tmp_path: Path) -> None:
    records = [
        {
            "timestamp": _iso(1_000),
            "payload": {
                "type": "custom_tool_call",
                "name": "wait",
                "call_id": "missing-cell",
                "input": json.dumps({"cell_id": "not-registered"}),
            },
        }
    ]
    _write_jsonl(tmp_path / "rollout.jsonl", records)

    invocations, audit = probe.parse_logs([tmp_path])

    assert invocations == []
    assert audit["wait_cell_miss"] == 1


def test_percentile_uses_type_7_linear_interpolation() -> None:
    values = [1.0, 2.0, 3.0, 4.0]

    assert probe.percentile(values, 0.5) == pytest.approx(2.5)
    assert probe.percentile(values, 0.9) == pytest.approx(3.7)
    assert probe.percentile(values, 0.99) == pytest.approx(3.97)


def test_timestamp_parser_rejects_timezone_less_values() -> None:
    with pytest.raises(ValueError, match="timezone"):
        probe.support.ts_seconds("2026-08-23T12:00:00")


def _invocation(*, llm_calls: int, excluded: str | None = None) -> probe.Invocation:
    return probe.Invocation(
        operation="close",
        target="sample-ticket",
        command="rebar transition sample-ticket in_progress closed",
        workdir=None,
        log="synthetic.jsonl",
        start=100,
        args=["transition", "sample-ticket", "in_progress", "closed"],
        excluded=excluded,
        end=200,
        exit_code=0,
        event={
            "llm_calls": llm_calls,
            "ran_model": "bedrock:test-model",
            "timestamp": 180,
            "total_ms": 50_000,
        },
    )


def test_cohort_requires_positive_llm_calls_and_requested_provider() -> None:
    included = _invocation(llm_calls=1)
    no_llm = _invocation(llm_calls=0)
    forced = _invocation(llm_calls=1, excluded="force")
    config = probe.ProbeConfig(
        log_roots=(),
        tracker=Path(".tickets-tracker"),
        since=50,
        until=250,
        provider_prefix="bedrock:",
    )

    assert probe.cohort([included, no_llm, forced], "close", config) == [included]


def test_close_phase_values_label_direct_metrics_and_residuals() -> None:
    direct = _invocation(llm_calls=1)
    direct.start = 0
    direct.end = 200
    direct.event = {
        "timestamp": 160,
        "total_ms": 100_000,
        "det_ms": 20_000,
        "llm_ms": 70_000,
        "status_at": 170,
        "signature_at": 180,
        "pre_verifier_total_ms": 120_000,
        "structural_scan_ms": 2_000,
        "material_policy_ms": 3_000,
        "descendant_scope_ms": 4_000,
        "landing_check_ms": 50_000,
        "verifier_call_ms": 110_000,
        "git_history_read_ms": 7_000,
        "alias_index_build_ms": 8_000,
        "ticket_ref_resolution_ms": 9_000,
        "diff_validation_ms": 10_000,
        "commits_inspected": 111,
        "distinct_references": 112,
        "descendant_ids": 113,
        "referencing_commits_found": 114,
    }
    legacy = _invocation(llm_calls=1)
    legacy.start = -200
    legacy.end = 20
    legacy.event = {"timestamp": -100, "total_ms": 0, "signature_at": 0}

    assert {
        "direct_phases": probe.close_phase_values(direct),
        "direct_workload": probe.close_workload_values(direct),
        "legacy_phases": probe.close_phase_values(legacy),
    } == {
        "direct_phases": {
            "pre_verifier_total": pytest.approx(120),
            "structural_scan": pytest.approx(2),
            "material_policy": pytest.approx(3),
            "descendant_scope": pytest.approx(4),
            "landing_check": pytest.approx(50),
            "verifier_call": pytest.approx(110),
            "git_history_read": pytest.approx(7),
            "alias_index_build": pytest.approx(8),
            "ticket_ref_resolution": pytest.approx(9),
            "diff_validation": pytest.approx(10),
            "legacy_uninstrumented": None,
            "deterministic_verifier": pytest.approx(20),
            "llm_verifier": pytest.approx(70),
            "verifier_overhead": pytest.approx(10),
            "verdict_to_status": pytest.approx(10),
            "status_to_signature": pytest.approx(10),
            "post_write_tail": pytest.approx(20),
        },
        "direct_workload": {
            "commits_inspected": 111,
            "distinct_references": 112,
            "descendant_ids": 113,
            "referencing_commits_found": 114,
        },
        "legacy_phases": {
            "pre_verifier_total": None,
            "structural_scan": None,
            "material_policy": None,
            "descendant_scope": None,
            "landing_check": None,
            "verifier_call": None,
            "git_history_read": None,
            "alias_index_build": None,
            "ticket_ref_resolution": None,
            "diff_validation": None,
            "legacy_uninstrumented": pytest.approx(100),
            "deterministic_verifier": None,
            "llm_verifier": None,
            "verifier_overhead": None,
            "verdict_to_status": None,
            "status_to_signature": None,
            "post_write_tail": pytest.approx(20),
        },
    }


def test_kaplan_meier_quantiles_account_for_tied_censoring() -> None:
    completed_at_100 = _invocation(llm_calls=1)
    completed_at_100.start = 0
    completed_at_100.end = 100
    censored_at_100 = _invocation(llm_calls=1)
    censored_at_100.start = 0
    censored_at_100.end = 100
    censored_at_100.exit_code = 130
    completed_at_200 = _invocation(llm_calls=1)
    completed_at_200.start = 0
    completed_at_200.end = 200
    censored_at_300 = _invocation(llm_calls=1)
    censored_at_300.start = 0
    censored_at_300.end = 300
    censored_at_300.exit_code = 130

    quantiles = probe.support.kaplan_meier_quantiles(
        [completed_at_100, censored_at_100, completed_at_200, censored_at_300]
    )

    assert quantiles == {"p50": pytest.approx(200), "p90": None, "p99": None}


def test_attach_events_enforces_window_and_prefers_nearest_end() -> None:
    invocation = _invocation(llm_calls=1)
    invocation.event = None
    events = [
        {"operation": "close", "ticket_id": "canonical", "timestamp": 196, "name": "early"},
        {"operation": "close", "ticket_id": "canonical", "timestamp": 202, "name": "near"},
        {"operation": "close", "ticket_id": "canonical", "timestamp": 204, "name": "outside"},
    ]
    config = probe.ProbeConfig(log_roots=(), tracker=Path(".tickets-tracker"))

    probe.support.attach_events(
        [invocation],
        {"sample-ticket": "canonical"},
        events,
        config,
    )

    assert invocation.event_matches == 2
    assert invocation.event is events[1]


def test_completion_phase_timestamps_read_only_matching_events(tmp_path: Path) -> None:
    status = {
        "timestamp": 170_000_000_000,
        "event_type": "STATUS",
        "data": {"status": "closed"},
    }
    signature = {
        "timestamp": 180_000_000_000,
        "event_type": "SIGNATURE",
        "data": {"kind": "completion-verifier"},
    }
    unrelated = {
        "timestamp": 175_000_000_000,
        "event_type": "SIGNATURE",
        "data": {"kind": "plan-review"},
    }
    for name, payload in (("status", status), ("signature", signature), ("other", unrelated)):
        (tmp_path / f"{name}.json").write_text(json.dumps(payload))

    phases = probe._completion_phase_timestamps(
        tmp_path,
        verdict_at=160,
        upper=200,
        audit=probe.Counter(),
    )

    assert phases == {"status_at": pytest.approx(170), "signature_at": pytest.approx(180)}


def test_completion_phase_timestamps_audit_non_integer_timestamps(tmp_path: Path) -> None:
    (tmp_path / ".cache.json").write_text(json.dumps({"state": {"status": "closed"}}))
    (tmp_path / "bad-timestamp.json").write_text(
        json.dumps(
            {
                "timestamp": "170000000000",
                "event_type": "STATUS",
                "data": {"status": "closed"},
            }
        )
    )
    audit = probe.Counter()

    phases = probe._completion_phase_timestamps(
        tmp_path,
        verdict_at=160,
        upper=200,
        audit=audit,
    )

    assert phases == {"status_at": None, "signature_at": None}
    assert audit["bad_phase_timestamp"] == 1


def test_unbounded_tracker_load_keeps_late_completion_events(tmp_path: Path) -> None:
    ticket_id = "1111-2222-3333-4444"
    ticket_dir = tmp_path / ticket_id
    ticket_dir.mkdir()
    (ticket_dir / ".cache.json").write_text(
        json.dumps({"state": {"ticket_id": ticket_id, "alias": "sample-ticket"}})
    )
    (ticket_dir / "verdict-COMPLETION_VERDICT.json").write_text(
        json.dumps(
            {
                "timestamp": 100_000_000_000,
                "event_type": "COMPLETION_VERDICT",
                "data": {
                    "ticket_id": ticket_id,
                    "verdict": "PASS",
                    "metrics": {"llm_calls": 1, "total_ms": 1_000},
                    "provider_provenance": {"ran_model": "bedrock:test-model"},
                },
            }
        )
    )
    (ticket_dir / "status.json").write_text(
        json.dumps(
            {
                "timestamp": 20_000_000_000_000,
                "event_type": "STATUS",
                "data": {"status": "closed"},
            }
        )
    )
    (ticket_dir / "signature.json").write_text(
        json.dumps(
            {
                "timestamp": 21_000_000_000_000,
                "event_type": "SIGNATURE",
                "data": {"kind": "completion-verifier"},
            }
        )
    )
    config = probe.ProbeConfig(log_roots=(), tracker=tmp_path)

    aliases, events, _audit = probe.support.load_tracker(config)

    assert aliases["sample-ticket"] == ticket_id
    assert events[0]["status_at"] == pytest.approx(20_000)
    assert events[0]["signature_at"] == pytest.approx(21_000)


def test_load_tracker_preserves_direct_timing_and_workload_metrics(tmp_path: Path) -> None:
    ticket_id = "1111-2222-3333-4444"
    ticket_dir = tmp_path / ticket_id
    ticket_dir.mkdir()
    direct_metrics = {
        "pre_verifier_total_ms": 101,
        "structural_scan_ms": 102,
        "material_policy_ms": 103,
        "descendant_scope_ms": 104,
        "landing_check_ms": 105,
        "verifier_call_ms": 106,
        "git_history_read_ms": 107,
        "alias_index_build_ms": 108,
        "ticket_ref_resolution_ms": 109,
        "diff_validation_ms": 110,
        "commits_inspected": 111,
        "distinct_references": 112,
        "descendant_ids": 113,
        "referencing_commits_found": 114,
    }
    (ticket_dir / "verdict-COMPLETION_VERDICT.json").write_text(
        json.dumps(
            {
                "timestamp": 100_000_000_000,
                "event_type": "COMPLETION_VERDICT",
                "data": {
                    "ticket_id": ticket_id,
                    "metrics": direct_metrics,
                },
            }
        )
    )

    _aliases, events, _audit = probe.support.load_tracker(
        probe.ProbeConfig(log_roots=(), tracker=tmp_path)
    )

    assert {key: events[0].get(key) for key in direct_metrics} == direct_metrics


def test_summary_reports_direct_timings_workload_and_legacy_residual(
    tmp_path: Path,
) -> None:
    ticket_id = "1111-2222-3333-4444"
    log_root = tmp_path / "logs"
    log_root.mkdir()
    tracker = tmp_path / "tracker"
    ticket_dir = tracker / ticket_id
    ticket_dir.mkdir(parents=True)
    source = (
        "const r = await tools.exec_command({cmd:`rtk rebar transition "
        "sample-ticket in_progress closed`, workdir:`/tmp`}); text(JSON.stringify(r));"
    )
    _write_jsonl(
        log_root / "rollout.jsonl",
        [
            {
                "timestamp": _iso(100),
                "payload": {
                    "type": "custom_tool_call",
                    "name": "exec",
                    "call_id": "close",
                    "input": source,
                },
            },
            {
                "timestamp": _iso(130),
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "close",
                    "output": json.dumps({"wall_time_seconds": 30, "exit_code": 0}),
                },
            },
        ],
    )
    (ticket_dir / ".cache.json").write_text(
        json.dumps({"state": {"ticket_id": ticket_id, "alias": "sample-ticket"}})
    )
    direct_timings = {
        "pre_verifier_total": ("pre_verifier_total_ms", 12_000),
        "structural_scan": ("structural_scan_ms", 1_000),
        "material_policy": ("material_policy_ms", 500),
        "descendant_scope": ("descendant_scope_ms", 250),
        "landing_check": ("landing_check_ms", 4_000),
        "verifier_call": ("verifier_call_ms", 6_000),
        "git_history_read": ("git_history_read_ms", 2_000),
        "alias_index_build": ("alias_index_build_ms", 500),
        "ticket_ref_resolution": ("ticket_ref_resolution_ms", 750),
        "diff_validation": ("diff_validation_ms", 250),
    }
    workload_counts = {
        "commits_inspected": 9,
        "distinct_references": 8,
        "descendant_ids": 7,
        "referencing_commits_found": 6,
    }
    legacy_metrics = {
        "llm_calls": 1,
        "total_ms": 5_000,
        "det_ms": 1_000,
        "llm_ms": 3_000,
    }
    sidecar_path = ticket_dir / "verdict-COMPLETION_VERDICT.json"

    def write_sidecar(metrics: dict[str, int]) -> None:
        sidecar_path.write_text(
            json.dumps(
                {
                    "timestamp": 120_000_000_000,
                    "event_type": "COMPLETION_VERDICT",
                    "data": {
                        "ticket_id": ticket_id,
                        "verdict": "PASS",
                        "metrics": metrics,
                        "provider_provenance": {"ran_model": "bedrock:test-model"},
                    },
                }
            )
        )

    command = [
        sys.executable,
        str(Path(__file__).resolve().parents[2] / "scripts" / "rebar_duration_probe.py"),
        "summary",
        "--log-root",
        str(log_root),
        "--tracker",
        str(tracker),
    ]
    write_sidecar(
        legacy_metrics
        | {field: value for field, value in direct_timings.values()}
        | workload_counts
    )
    direct = subprocess.run(command, check=True, capture_output=True, text=True)
    write_sidecar(legacy_metrics)
    legacy = subprocess.run(command, check=True, capture_output=True, text=True)

    expected_direct_lines = {
        f"{phase}\tsource=direct-sidecar-metric({field})\tn=1\t"
        f"p50/p90/p99={milliseconds / 1000:.3f}/{milliseconds / 1000:.3f}/"
        f"{milliseconds / 1000:.3f}"
        for phase, (field, milliseconds) in direct_timings.items()
    } | {
        f"{field}\tsource=direct-sidecar-count({field})\tn=1\t"
        f"p50/p90/p99={value:.3f}/{value:.3f}/{value:.3f}"
        for field, value in workload_counts.items()
    }
    expected_legacy_line = (
        "legacy_uninstrumented\t"
        "source=unexplained-legacy-residual(no-causal-attribution)\t"
        "n=1\tp50/p90/p99=15.000/15.000/15.000"
    )
    direct_lines = set(direct.stdout.splitlines())
    legacy_lines = set(legacy.stdout.splitlines())

    assert {
        "missing_direct_lines": expected_direct_lines - direct_lines,
        "direct_has_legacy_residual": any(
            line.startswith("legacy_uninstrumented\t") for line in direct_lines
        ),
        "missing_legacy_lines": {expected_legacy_line} - legacy_lines,
        "old_residual_label_present": "pre_verifier_residual" in direct.stdout + legacy.stdout,
    } == {
        "missing_direct_lines": set(),
        "direct_has_legacy_residual": False,
        "missing_legacy_lines": set(),
        "old_residual_label_present": False,
    }


def test_default_paths_are_repository_neutral() -> None:
    assert probe.DEFAULT_LOG_ROOTS == (
        Path.home() / ".codex" / "sessions",
        Path.home() / ".codex" / "archived_sessions",
    )
    args = probe._parser().parse_args([])

    assert args.tracker == Path(".tickets-tracker")
    assert args.log_root is None
    assert args.since is None
    assert args.until is None
    assert args.provider_prefix is None
    assert args.current_since is None


def test_resolve_target_uses_output_and_canonical_id_fallbacks() -> None:
    from_output = _invocation(llm_calls=1)
    from_output.target = "not-an-alias"
    from_output.output.append('{"ticket_id":"1111-2222-3333-4444"}')
    canonical = _invocation(llm_calls=1)
    canonical.target = "aaaa-bbbb-cccc-dddd"

    assert probe.support.resolve_target(from_output, {}) == "1111-2222-3333-4444"
    assert probe.support.resolve_target(canonical, {}) == "aaaa-bbbb-cccc-dddd"


def test_main_rejects_inverted_time_bounds(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        probe.sys,
        "argv",
        [
            "rebar_duration_probe.py",
            "--since",
            "2026-08-24T00:00:00Z",
            "--until",
            "2026-08-23T00:00:00Z",
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        probe.main()

    assert exc_info.value.code == 2
    assert "--since must not be later than --until" in capsys.readouterr().err
