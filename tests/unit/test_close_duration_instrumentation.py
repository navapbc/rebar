"""Completion close-gate duration instrumentation contract."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

_TESTS_DIR = Path(__file__).resolve().parents[1]
_INTERFACES_DIR = _TESTS_DIR / "interfaces"
for _path in (_TESTS_DIR, _INTERFACES_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from interfaces.conftest import (  # noqa: E402
    _rebar_repo_template as _rebar_repo_template,
)
from interfaces.conftest import rebar_repo as rebar_repo  # noqa: E402

import rebar  # noqa: E402
import rebar.llm  # noqa: E402
from rebar._commands import (  # noqa: E402
    close_precheck,
    transition_close,
    verify_commit,
)
from rebar._engine_support import (  # noqa: E402
    commit_impact,
    descendants,
    resolver,
)
from rebar.llm import completion_sidecar  # noqa: E402


def test_model_backed_pass_close_preserves_consumption_and_emits_direct_metrics(
    rebar_repo: Path, monkeypatch
) -> None:
    sentinel_metrics = {"requests": 7, "tool_calls": 11, "total_ms": 13_000}
    timing_fields = (
        "pre_verifier_total_ms",
        "structural_scan_ms",
        "material_policy_ms",
        "descendant_scope_ms",
        "landing_check_ms",
        "verifier_call_ms",
        "git_history_read_ms",
        "alias_index_build_ms",
        "ticket_ref_resolution_ms",
        "diff_validation_ms",
    )
    workload_counts = (
        "commits_inspected",
        "distinct_references",
        "descendant_ids",
        "referencing_commits_found",
    )
    (rebar_repo / "rebar.toml").write_text(
        "[verify]\nrequire_completion_verification_for_close = true\n"
    )

    def model_backed_pass(ticket_id: str, **kwargs) -> dict:
        return {
            "verdict": "PASS",
            "findings": [],
            "criteria": [],
            "runner": "fake-agent-runner",
            "model": "fake-model",
            "metrics": dict(sentinel_metrics),
        }

    monkeypatch.setattr(rebar.llm, "verify_completion", model_backed_pass)
    ticket = rebar.create_ticket(
        "task",
        "persist close duration instrumentation",
        description=(
            "## Acceptance Criteria\n"
            "- [x] direct close duration metrics are persisted\n\n"
            "## Context\n"
            "Exercise the public close boundary.\n"
        ),
        repo_root=str(rebar_repo),
    )
    rebar.transition(ticket, "open", "in_progress", repo_root=str(rebar_repo))

    rebar.transition(ticket, "in_progress", "closed", repo_root=str(rebar_repo))
    record = completion_sidecar.latest_pass_record(ticket, repo_root=str(rebar_repo))
    metrics = (record or {}).get("metrics", {})

    assert {
        "preserved": {key: metrics.get(key) for key in sentinel_metrics},
        "all_direct_metrics_are_non_negative_ints": all(
            type(metrics.get(key)) is int and metrics[key] >= 0
            for key in timing_fields + workload_counts
        ),
    } == {
        "preserved": sentinel_metrics,
        "all_direct_metrics_are_non_negative_ints": True,
    }


def test_model_backed_fail_close_preserves_consumption_and_emits_direct_metrics(
    rebar_repo: Path, monkeypatch
) -> None:
    sentinel_metrics = {"requests": 7, "tool_calls": 11, "total_ms": 13_000}
    timing_fields = (
        "pre_verifier_total_ms",
        "structural_scan_ms",
        "material_policy_ms",
        "descendant_scope_ms",
        "landing_check_ms",
        "verifier_call_ms",
        "git_history_read_ms",
        "alias_index_build_ms",
        "ticket_ref_resolution_ms",
        "diff_validation_ms",
    )
    workload_counts = (
        "commits_inspected",
        "distinct_references",
        "descendant_ids",
        "referencing_commits_found",
    )
    verifier_calls = []
    (rebar_repo / "rebar.toml").write_text(
        "[verify]\nrequire_completion_verification_for_close = true\n"
    )

    def model_backed_fail(ticket_id: str, **kwargs) -> dict:
        verifier_calls.append(ticket_id)
        return {
            "verdict": "FAIL",
            "findings": [
                {
                    "criterion": "AC1",
                    "detail": "Add the missing close timing fields before retrying.",
                    "severity": "high",
                    "dimension": "completion",
                }
            ],
            "criteria": [],
            "runner": "fake-agent-runner",
            "model": "fake-model",
            "metrics": dict(sentinel_metrics),
        }

    monkeypatch.setattr(rebar.llm, "verify_completion", model_backed_fail)
    ticket = rebar.create_ticket(
        "task",
        "persist failed close duration instrumentation",
        description=(
            "## Acceptance Criteria\n"
            "- [x] failed close duration metrics are persisted\n\n"
            "## Context\n"
            "Exercise the public blocked-close boundary.\n"
        ),
        repo_root=str(rebar_repo),
    )
    rebar.transition(ticket, "open", "in_progress", repo_root=str(rebar_repo))

    try:
        rebar.transition(ticket, "in_progress", "closed", repo_root=str(rebar_repo))
    except rebar.RebarError as exc:
        close_error = exc
    else:
        close_error = None
    record = completion_sidecar.latest_fail_verdict(ticket, repo_root=str(rebar_repo))
    metrics = (record or {}).get("metrics", {})

    assert {
        "raised_rebar_error": isinstance(close_error, rebar.RebarError),
        "status": rebar.show_ticket(ticket, repo_root=str(rebar_repo))["status"],
        "verifier_calls": verifier_calls,
        "preserved": {key: metrics.get(key) for key in sentinel_metrics},
        "all_direct_metrics_are_non_negative_ints": all(
            type(metrics.get(key)) is int and metrics[key] >= 0
            for key in timing_fields + workload_counts
        ),
    } == {
        "raised_rebar_error": True,
        "status": "in_progress",
        "verifier_calls": [ticket],
        "preserved": sentinel_metrics,
        "all_direct_metrics_are_non_negative_ints": True,
    }


def test_referencing_commits_optionally_records_found_count_without_an_extra_scan(
    monkeypatch,
) -> None:
    calls = []
    outer_metrics = {"sentinel": 7}

    def scan(
        ticket_ids: set[str],
        tracker: str,
        repo_root: str,
        metrics: dict[str, int] | None = None,
    ) -> list[str]:
        calls.append((ticket_ids, tracker, repo_root, metrics is outer_metrics))
        return ["newest", "oldest"]

    monkeypatch.setattr(commit_impact, "referencing_commits", scan)

    found = close_precheck._referencing_commits(
        {"ticket-id"}, "/tracker", "/repo", metrics=outer_metrics
    )

    assert {"found": found, "metrics": outer_metrics, "calls": calls} == {
        "found": ["newest", "oldest"],
        "metrics": {"sentinel": 7, "referencing_commits_found": 2},
        "calls": [({"ticket-id"}, "/tracker", "/repo", True)],
    }


def test_commit_impact_referencing_commits_records_metrics_without_repeating_work(
    monkeypatch,
) -> None:
    calls = {"git": [], "index": [], "extract": [], "resolve": []}
    history_splits = []
    alias_index = {"accepted-alias": ["ticket-id"]}
    dir_names = ["other-id", "ticket-id"]
    scan_index = SimpleNamespace(alias_to_dirs=alias_index, sorted_dir_names=dir_names)

    class CountingHistory(str):
        def split(self, sep=None, maxsplit=-1):
            history_splits.append(sep)
            return super().split(sep, maxsplit)

    history = CountingHistory(
        "sha-one\x1faccepted-alias\x00sha-two\x1fother-alias\x00sha-three\x1fother-alias\x00"
    )

    def git_log(command, **kwargs):
        calls["git"].append(command)
        return SimpleNamespace(returncode=0, stdout=history)

    def build_index(tracker: str):
        calls["index"].append(tracker)
        return scan_index

    def extract_refs(message: str) -> list[str]:
        calls["extract"].append(message)
        return [message]

    def resolve_ref(ref: str, tracker: str, **kwargs) -> str:
        calls["resolve"].append(
            (
                ref,
                tracker,
                kwargs.get("quiet"),
                kwargs.get("alias_index") is alias_index,
                kwargs.get("dir_names") is dir_names,
            )
        )
        return {"accepted-alias": "ticket-id", "other-alias": "other-id"}[ref]

    monkeypatch.setattr(commit_impact.subprocess, "run", git_log)
    monkeypatch.setattr(resolver, "build_resolver_scan_index", build_index)
    monkeypatch.setattr(resolver, "resolve_ticket_id", resolve_ref)
    monkeypatch.setattr(verify_commit, "extract_ticket_refs", extract_refs)
    metrics = {"sentinel": 7}

    found = commit_impact.referencing_commits({"ticket-id"}, "/tracker", "/repo", metrics=metrics)

    assert {
        "found": found,
        "sentinel": metrics.get("sentinel"),
        "timings_are_non_negative_ints": all(
            type(metrics.get(key)) is int and metrics[key] >= 0
            for key in (
                "git_history_read_ms",
                "alias_index_build_ms",
                "ticket_ref_resolution_ms",
            )
        ),
        "workload": {
            "commits_inspected": metrics.get("commits_inspected"),
            "distinct_references": metrics.get("distinct_references"),
        },
        "calls": calls,
        "history_splits": history_splits,
    } == {
        "found": ["sha-one"],
        "sentinel": 7,
        "timings_are_non_negative_ints": True,
        "workload": {"commits_inspected": 3, "distinct_references": 2},
        "calls": {
            "git": [["git", "-C", "/repo", "log", "--format=%H%x1f%B%x00"]],
            "index": ["/tracker"],
            "extract": ["accepted-alias", "other-alias", "other-alias"],
            "resolve": [
                ("accepted-alias", "/tracker", True, True, True),
                ("other-alias", "/tracker", True, True, True),
            ],
        },
        "history_splits": ["\x00"],
    }


def test_check_file_impact_vs_diff_records_duration_without_repeating_validation(
    monkeypatch,
) -> None:
    calls = {"union": [], "attached": [], "merge": [], "changed": [], "undeclared": []}
    accepted_ids = {"ticket-id"}
    impact = ["src/owned.py"]

    def union_file_impact(ticket_ids: set[str], tracker: str) -> list[str]:
        calls["union"].append((ticket_ids, tracker))
        return impact

    def attached_commit_shas(ticket_ids: set[str], tracker: str) -> list[str]:
        calls["attached"].append((ticket_ids, tracker))
        return []

    def is_merge_commit(sha: str, repo_root: str) -> bool:
        calls["merge"].append((sha, repo_root))
        return False

    def changed_paths(sha: str, repo_root: str) -> list[str]:
        calls["changed"].append((sha, repo_root))
        return ["src/owned.py"]

    def undeclared_paths(paths: list[str], declared: list[str], *, repo_root: str) -> list[str]:
        calls["undeclared"].append((paths, declared, repo_root))
        return []

    monkeypatch.setattr(close_precheck, "_union_file_impact", union_file_impact)
    monkeypatch.setattr(close_precheck, "_attached_commit_shas", attached_commit_shas)
    monkeypatch.setattr(commit_impact, "is_merge_commit", is_merge_commit)
    monkeypatch.setattr(commit_impact, "changed_paths", changed_paths)
    monkeypatch.setattr(commit_impact, "undeclared_paths", undeclared_paths)
    metrics = {"sentinel": 7}

    result = close_precheck._check_file_impact_vs_diff(
        accepted_ids, ["ref-sha"], "/tracker", "/repo", metrics=metrics
    )

    assert {
        "result": result,
        "sentinel": metrics.get("sentinel"),
        "duration_is_non_negative_int": (
            type(metrics.get("diff_validation_ms")) is int and metrics["diff_validation_ms"] >= 0
        ),
        "calls": calls,
    } == {
        "result": None,
        "sentinel": 7,
        "duration_is_non_negative_int": True,
        "calls": {
            "union": [(accepted_ids, "/tracker")],
            "attached": [(accepted_ids, "/tracker")],
            "merge": [("ref-sha", "/repo")],
            "changed": [("ref-sha", "/repo")],
            "undeclared": [(["src/owned.py"], impact, "/repo")],
        },
    }


def test_check_work_landed_records_duration_and_threads_metrics_once(monkeypatch) -> None:
    from rebar._engine_support import field_reads

    calls = {"union": [], "referencing": [], "file_impact": [], "diff": []}
    accepted_ids = {"ticket-id", "child-id"}
    outer_metrics = {"sentinel": 7}

    def union_file_impact(ticket_ids: set[str], tracker: str) -> list[str]:
        calls["union"].append((ticket_ids, tracker))
        return ["src/owned.py"]

    def referencing_commits(
        ticket_ids: set[str],
        tracker: str,
        repo_root: str,
        metrics: dict[str, int] | None = None,
    ) -> list[str]:
        calls["referencing"].append((ticket_ids, tracker, repo_root, metrics is outer_metrics))
        if metrics is not None:
            metrics["history_child"] = 11
        return ["ref-sha"]

    def file_impact(ticket_id: str, tracker: str) -> list[str]:
        calls["file_impact"].append((ticket_id, tracker))
        return ["src/owned.py"]

    def validate_diff(
        ticket_ids: set[str],
        referencing: list[str],
        tracker: str,
        code_root: str,
        metrics: dict[str, int] | None = None,
    ) -> None:
        calls["diff"].append(
            (ticket_ids, referencing, tracker, code_root, metrics is outer_metrics)
        )
        if metrics is not None:
            metrics["diff_child"] = 13

    monkeypatch.setattr(close_precheck, "_union_file_impact", union_file_impact)
    monkeypatch.setattr(close_precheck, "_referencing_commits", referencing_commits)
    monkeypatch.setattr(field_reads, "file_impact", file_impact)
    monkeypatch.setattr(close_precheck, "_check_file_impact_vs_diff", validate_diff)

    result = close_precheck._check_work_landed(
        "ticket-id",
        "resolved-id",
        accepted_ids,
        "/tracker",
        "/repo",
        metrics=outer_metrics,
    )

    assert {
        "result": result,
        "metrics": outer_metrics,
        "duration_is_non_negative_int": (
            type(outer_metrics.get("landing_check_ms")) is int
            and outer_metrics["landing_check_ms"] >= 0
        ),
        "calls": calls,
    } == {
        "result": None,
        "metrics": {
            "sentinel": 7,
            "history_child": 11,
            "diff_child": 13,
            "landing_check_ms": outer_metrics.get("landing_check_ms"),
        },
        "duration_is_non_negative_int": True,
        "calls": {
            "union": [(accepted_ids, "/tracker")],
            "referencing": [(accepted_ids, "/tracker", "/repo", True)],
            "file_impact": [("ticket-id", "/tracker")],
            "diff": [(accepted_ids, ["ref-sha"], "/tracker", "/repo", True)],
        },
    }


def test_resolved_completion_scope_records_unique_descendants_without_repeated_resolution(
    monkeypatch,
) -> None:
    calls = {"descendants": [], "resolve": []}
    descendant_map = {
        "epics": ["epic-alias"],
        "stories": ["story-alias", "missing-alias"],
        "tasks": ["duplicate-alias"],
        "bugs": [],
    }

    def list_scope(ticket_id: str, tracker: str) -> dict[str, list[str]]:
        calls["descendants"].append((ticket_id, tracker))
        return descendant_map

    def resolve(ticket_id: str, tracker: str) -> str | None:
        calls["resolve"].append((ticket_id, tracker))
        return {
            "root-alias": "root-id",
            "epic-alias": "epic-id",
            "story-alias": "story-id",
            "missing-alias": None,
            "duplicate-alias": "epic-id",
        }[ticket_id]

    monkeypatch.setattr(descendants, "list_descendants", list_scope)
    monkeypatch.setattr(resolver, "resolve_ticket_id", resolve)
    outer_metrics = {"sentinel": 7}

    resolved_id, accepted_ids = close_precheck._resolved_completion_scope(
        "root-alias", "/tracker", metrics=outer_metrics
    )

    assert {
        "resolved_id": resolved_id,
        "accepted_ids": accepted_ids,
        "metrics": outer_metrics,
        "duration_is_non_negative_int": (
            type(outer_metrics.get("descendant_scope_ms")) is int
            and outer_metrics["descendant_scope_ms"] >= 0
        ),
        "calls": calls,
    } == {
        "resolved_id": "root-id",
        "accepted_ids": {"root-id", "epic-id", "story-id"},
        "metrics": {
            "sentinel": 7,
            "descendant_ids": 2,
            "descendant_scope_ms": outer_metrics.get("descendant_scope_ms"),
        },
        "duration_is_non_negative_int": True,
        "calls": {
            "descendants": [("root-alias", "/tracker")],
            "resolve": [
                ("root-alias", "/tracker"),
                ("epic-alias", "/tracker"),
                ("story-alias", "/tracker"),
                ("missing-alias", "/tracker"),
                ("duplicate-alias", "/tracker"),
            ],
        },
    }


def test_verify_with_duration_metrics_merges_direct_metrics_without_repeating_verifier(
    monkeypatch,
) -> None:
    from rebar._commands import close_autoresume

    calls = {"verify": [], "clock": []}
    consumption_metrics = {"requests": 7, "tool_calls": 11, "total_ms": 13_000}

    def verify(
        ticket_id: str, *, ref: str | None, repo_root: str, cfg_root: str
    ) -> dict[str, object]:
        calls["verify"].append((ticket_id, ref, repo_root, cfg_root))
        return {"verdict": "PASS", "metrics": consumption_metrics}

    ticks = iter((4_000_000_000, 6_500_000_000))

    def monotonic_ns() -> int:
        tick = next(ticks)
        calls["clock"].append(tick)
        return tick

    monkeypatch.setattr(close_autoresume, "verify_with_auto_resume", verify)
    monkeypatch.setattr(close_precheck.time, "monotonic_ns", monotonic_ns)
    collector = {
        "_pre_verifier_started_ns": 1_000_000_000,
        "collector_sentinel": 17,
        "descendant_scope_ms": 23,
        "descendant_ids": 2,
        "landing_check_ms": 31,
        "diff_validation_ms": 19,
        "referencing_commits_found": 1,
    }

    result = close_precheck._verify_with_duration_metrics(
        "ticket-id", ref="verified-sha", code_root="/repo", metrics=collector
    )

    assert {
        "result": result,
        "collector": collector,
        "same_metrics_object": result["metrics"] is consumption_metrics,
        "calls": calls,
    } == {
        "result": {
            "verdict": "PASS",
            "metrics": {
                "requests": 7,
                "tool_calls": 11,
                "total_ms": 13_000,
                "collector_sentinel": 17,
                "descendant_scope_ms": 23,
                "descendant_ids": 2,
                "landing_check_ms": 31,
                "diff_validation_ms": 19,
                "referencing_commits_found": 1,
                "pre_verifier_total_ms": 3_000,
                "verifier_call_ms": 2_500,
            },
        },
        "collector": {
            "collector_sentinel": 17,
            "descendant_scope_ms": 23,
            "descendant_ids": 2,
            "landing_check_ms": 31,
            "diff_validation_ms": 19,
            "referencing_commits_found": 1,
            "pre_verifier_total_ms": 3_000,
            "verifier_call_ms": 2_500,
        },
        "same_metrics_object": True,
        "calls": {
            "verify": [("ticket-id", "verified-sha", "/repo", "/repo")],
            "clock": [4_000_000_000, 6_500_000_000],
        },
    }


def test_completion_precheck_threads_one_collector_through_pass_and_sidecar(
    monkeypatch,
) -> None:
    calls = {"scope": [], "landing": [], "verify": [], "sidecar": []}
    outer_metrics = {"collector_sentinel": 7}
    accepted_ids = {"root-id", "child-id"}
    consumption_metrics = {"requests": 5, "tool_calls": 3, "total_ms": 2_000}

    def resolve_scope(
        ticket_id: str, tracker: str, *, metrics: dict[str, int] | None = None
    ) -> tuple[str, set[str]]:
        calls["scope"].append((ticket_id, tracker, metrics is outer_metrics))
        if metrics is not None:
            metrics["scope_child"] = 11
        return "root-id", accepted_ids

    def check_landing(
        ticket_id: str,
        resolved_id: str,
        ticket_ids: set[str],
        tracker: str,
        code_root: str,
        *,
        metrics: dict[str, int] | None = None,
    ) -> None:
        calls["landing"].append(
            (
                ticket_id,
                resolved_id,
                ticket_ids,
                tracker,
                code_root,
                metrics is outer_metrics,
            )
        )
        if metrics is not None:
            metrics["landing_child"] = 13

    def verify(
        ticket_id: str,
        *,
        ref: str | None,
        code_root: str,
        metrics: dict[str, int] | None = None,
        ticket_view=None,
        ticket_read_mode: str | None = None,
    ) -> dict[str, object]:
        assert ticket_view is None
        assert ticket_read_mode is None
        calls["verify"].append((ticket_id, ref, code_root, metrics is outer_metrics))
        if metrics is not None:
            metrics["verifier_child"] = 17
            consumption_metrics.update(metrics)
        return {
            "verdict": "PASS",
            "findings": [],
            "criteria": [],
            "runner": "fake-agent-runner",
            "model": "fake-model",
            "metrics": consumption_metrics,
        }

    def emit_sidecar(
        completion_sidecar,
        result: dict[str, object],
        ticket_id: str,
        repo_root: str,
        *,
        is_pass: bool,
    ) -> None:
        calls["sidecar"].append(
            (
                ticket_id,
                repo_root,
                is_pass,
                result["metrics"] is consumption_metrics,
                dict(result["metrics"]),
            )
        )

    monkeypatch.setattr(close_precheck.config, "repo_root", lambda repo_root: "/code")
    monkeypatch.setattr(close_precheck.config, "tracker_dir", lambda repo_root: "/tracker")
    monkeypatch.setattr(close_precheck, "_gate_skip_expectation", lambda *args: None)
    monkeypatch.setattr(close_precheck.txn, "close_class_refusal", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        close_precheck,
        "_administrative_disposition",
        lambda *args: close_precheck._NO_DISPOSITION,
    )
    monkeypatch.setattr(close_precheck, "_ensure_duplicate_close_is_linked", lambda *args: None)
    monkeypatch.setattr(close_precheck.txn, "ensure_ac_boxes_checked", lambda *args: None)
    monkeypatch.setattr(close_precheck.txn, "ensure_attested_items_valid", lambda *args: None)
    monkeypatch.setattr(close_precheck, "_resolved_completion_scope", resolve_scope)
    monkeypatch.setattr(close_precheck, "_check_work_landed", check_landing)
    monkeypatch.setattr(close_precheck, "_verify_with_duration_metrics", verify)
    monkeypatch.setattr(close_precheck, "_emit_completion_sidecar", emit_sidecar)

    outcome = close_precheck._completion_precheck(
        "ticket-alias",
        "task",
        "/cfg",
        "/repo",
        reason="",
        force_close="",
        ref="verified-sha",
        metrics=outer_metrics,
    )

    expected_merged_metrics = {
        "requests": 5,
        "tool_calls": 3,
        "total_ms": 2_000,
        "collector_sentinel": 7,
        "scope_child": 11,
        "landing_child": 13,
        "verifier_child": 17,
    }
    assert {
        "outcome": outcome,
        "collector": outer_metrics,
        "calls": calls,
    } == {
        "outcome": (
            {
                "verdict": "PASS",
                "findings": [],
                "criteria": [],
                "runner": "fake-agent-runner",
                "model": "fake-model",
                "metrics": expected_merged_metrics,
                "ticket_id": "root-id",
            },
            "required",
        ),
        "collector": {
            "collector_sentinel": 7,
            "scope_child": 11,
            "landing_child": 13,
            "verifier_child": 17,
        },
        "calls": {
            "scope": [("ticket-alias", "/tracker", True)],
            "landing": [
                (
                    "ticket-alias",
                    "root-id",
                    accepted_ids,
                    "/tracker",
                    "/code",
                    True,
                )
            ],
            "verify": [("ticket-alias", "verified-sha", "/code", True)],
            "sidecar": [("ticket-alias", "/repo", True, True, expected_merged_metrics)],
        },
    }


def test_close_metrics_default_all_fields_and_time_one_existing_phase(monkeypatch) -> None:
    calls = {"clock": [], "operation": []}
    ticks = iter((1_000_000_000, 2_000_000_000, 5_500_000_000))
    operation_result = object()

    def monotonic_ns() -> int:
        tick = next(ticks)
        calls["clock"].append(tick)
        return tick

    def operation(ticket_id: str, *, tracker: str, include_children: bool) -> object:
        calls["operation"].append((ticket_id, tracker, include_children))
        return operation_result

    monkeypatch.setattr(
        transition_close,
        "time",
        SimpleNamespace(monotonic_ns=monotonic_ns),
        raising=False,
    )

    metrics = transition_close._new_close_metrics()
    result = transition_close._timed_close_phase(
        metrics,
        "structural_scan_ms",
        operation,
        "ticket-id",
        tracker="/tracker",
        include_children=True,
    )

    assert {"result": result, "metrics": metrics, "calls": calls} == {
        "result": operation_result,
        "metrics": {
            "_pre_verifier_started_ns": 1_000_000_000,
            "pre_verifier_total_ms": 0,
            "structural_scan_ms": 3_500,
            "material_policy_ms": 0,
            "descendant_scope_ms": 0,
            "landing_check_ms": 0,
            "verifier_call_ms": 0,
            "git_history_read_ms": 0,
            "alias_index_build_ms": 0,
            "ticket_ref_resolution_ms": 0,
            "diff_validation_ms": 0,
            "commits_inspected": 0,
            "distinct_references": 0,
            "descendant_ids": 0,
            "referencing_commits_found": 0,
        },
        "calls": {
            "clock": [1_000_000_000, 2_000_000_000, 5_500_000_000],
            "operation": [("ticket-id", "/tracker", True)],
        },
    }
