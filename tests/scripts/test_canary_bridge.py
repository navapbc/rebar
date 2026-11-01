"""Test canary heartbeat and binding-drift classification and alert lifecycles.

All gh and rebar calls use the injected runner. Environment and time are injected.
Lifecycle writes fail loudly, dedup reads fail softly, and bridge fsck exit 1 with
valid finding JSON is signal data. Invalid or partial JSON fails closed.

Held-out oracles merged from test_canary_bridge_heldout: HELD-OUT suite for
scripts/canary_bridge.py (ticket e602) — not shown to the implementer. Restored into tests/scripts/
before validation.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


_SCRIPT = REPO_ROOT / "scripts" / "canary_bridge.py"


@pytest.fixture(scope="module")
def mod() -> ModuleType:
    spec = importlib.util.spec_from_file_location("canary_bridge", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeRunner:
    """Records every argv; replies from a prefix->response table.

    Response is ``(returncode, stdout, stderr)``. The longest matching argv
    prefix (joined with spaces) wins; unmatched argv gets ``(0, "", "")``.
    """

    def __init__(self, responses: dict[tuple[str, ...], tuple[int, str, str]] | None = None):
        self.calls: list[list[str]] = []
        self.responses = responses or {}

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        self.calls.append(list(argv))
        best: tuple[str, ...] | None = None
        for prefix in self.responses:
            if tuple(argv[: len(prefix)]) == prefix and (best is None or len(prefix) > len(best)):
                best = prefix
        return self.responses[best] if best is not None else (0, "", "")

    def rebar_calls(self) -> list[list[str]]:
        return [c for c in self.calls if c and c[0] == "rebar"]

    def rebar_writes(self) -> list[list[str]]:
        return [c for c in self.rebar_calls() if c[1] in ("create", "comment", "transition")]


def read_outputs(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            out[k] = v
    return out


NOW = 1_785_800_000


NEVER_RUN: dict[tuple[str, ...], tuple[int, str, str]] = {
    ("rebar", "bridge", "status"): (1, '{"verdict": "NEVER_RUN"}', ""),
}


def hb_env(tmp_path: Path, **over: str) -> dict[str, str]:
    env = {
        "ALERT_WINDOW_HOURS": "2",
        "GITHUB_REPOSITORY": "navapbc/rebar",
        "GITHUB_OUTPUT": str(tmp_path / "gh_out"),
    }
    env.update(over)
    (tmp_path / "gh_out").touch()
    return env


def _iso(epoch: int) -> str:
    import datetime as dt

    return dt.datetime.fromtimestamp(epoch, tz=dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_check_heartbeat_fresh(mod: ModuleType, tmp_path: Path) -> None:
    """A success run inside the window -> stale=false + healthy message."""
    run_epoch = NOW - 3600 - 120  # 1h 2m ago, window 2h
    payload = json.dumps({"updated_at": _iso(run_epoch)})
    runner = FakeRunner({("gh", "api"): (0, payload, ""), **NEVER_RUN})
    env = hb_env(tmp_path)
    rc = mod.main(["check-heartbeat"], runner=runner, environ=env, now_epoch=NOW)
    assert rc == 0
    out = read_outputs(tmp_path / "gh_out")
    assert out["stale"] == "false"
    assert out["last_run_ago"] == "1h 2m ago"
    assert out["status_msg"] == (
        "Reconciler healthy — last successful run was 1h 2m ago. "
        "(last-pass producer bootstrap fallback)."
    )
    # the gh api call targets the reconcile-bridge workflow's success runs
    gh = [c for c in runner.calls if c[0] == "gh"]
    assert len(gh) == 1
    assert (
        "repos/navapbc/rebar/actions/workflows/reconcile-bridge.yml/runs?status=success&per_page=1"
    ) in gh[0]


def test_check_heartbeat_stale(mod: ModuleType, tmp_path: Path) -> None:
    """A success run older than the window -> stale=true + threshold message."""
    run_epoch = NOW - (3 * 3600) - (5 * 60)  # 3h 5m ago
    payload = json.dumps({"updated_at": _iso(run_epoch)})
    runner = FakeRunner({("gh", "api"): (0, payload, ""), **NEVER_RUN})
    env = hb_env(tmp_path)
    rc = mod.main(["check-heartbeat"], runner=runner, environ=env, now_epoch=NOW)
    assert rc == 0
    out = read_outputs(tmp_path / "gh_out")
    assert out["stale"] == "true"
    assert out["last_run_ago"] == "3h 5m ago"
    assert out["status_msg"] == "Last successful run was 3h 5m ago (threshold: 2h)."


def test_check_heartbeat_no_success_runs(mod: ModuleType, tmp_path: Path) -> None:
    """Empty API result (legit zero-state) -> stale=true, last_run_ago=never."""
    runner = FakeRunner({("gh", "api"): (0, "", ""), **NEVER_RUN})
    env = hb_env(tmp_path)
    rc = mod.main(["check-heartbeat"], runner=runner, environ=env, now_epoch=NOW)
    assert rc == 0
    out = read_outputs(tmp_path / "gh_out")
    assert out["stale"] == "true"
    assert out["last_run_ago"] == "never"
    assert out["status_msg"] == "No successful reconcile-bridge.yml runs found."


def test_check_heartbeat_api_error_is_transient(
    mod: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """API failure must NOT alert: stale=false + indeterminate message + ::warning."""
    runner = FakeRunner({("gh", "api"): (1, "", "boom: rate limited"), **NEVER_RUN})
    env = hb_env(tmp_path)
    rc = mod.main(["check-heartbeat"], runner=runner, environ=env, now_epoch=NOW)
    assert rc == 0
    out = read_outputs(tmp_path / "gh_out")
    assert out["stale"] == "false"
    assert out["last_run_ago"] == "unknown"
    assert out["status_msg"] == (
        "GitHub Actions API error — heartbeat indeterminate, treating as transient."
    )
    assert "::warning::" in capsys.readouterr().out


def test_check_heartbeat_invalid_window(
    mod: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A non-positive-integer window is a configuration error: exit 1 + ::error."""
    runner = FakeRunner()
    env = hb_env(tmp_path, ALERT_WINDOW_HOURS="nope")
    rc = mod.main(["check-heartbeat"], runner=runner, environ=env, now_epoch=NOW)
    assert rc == 1
    assert "::error::" in capsys.readouterr().out
    assert runner.calls == []


def alert_env(tmp_path: Path, **over: str) -> dict[str, str]:
    env = {
        "DRY_RUN": "false",
        "ALERT_TAG": "heartbeat-alert",
        "ALERT_WINDOW_HOURS": "2",
        "STALE": "true",
        "LAST_RUN_AGO": "3h 5m ago",
        "STATUS_MSG": "Last successful run was 3h 5m ago (threshold: 2h).",
        "RUN_URL": "https://github.com/navapbc/rebar/actions/runs/42",
        "GITHUB_REPOSITORY": "navapbc/rebar",
        "GITHUB_RUN_ID": "42",
        "CANARY_WORKFLOW_FILE": "reconcile-bridge-canary.yml",
    }
    env.update(over)
    return env


def prev_runs(*conclusions: str, start_id: int = 41) -> tuple[int, str, str]:
    """A gh api run-history response listing prior completed canary runs."""
    runs = [
        {"id": start_id - i, "conclusion": c, "updated_at": _iso(NOW - 1200 * (i + 1))}
        for i, c in enumerate(conclusions)
    ]
    return (0, json.dumps(runs), "")


def show_with_comments(*bodies_and_ages: tuple[str, int]) -> tuple[int, str, str]:
    """A `rebar show --output json` response whose comments have given (body, age-secs)."""
    comments = [{"body": body, "timestamp": (NOW - age) * 10**9} for body, age in bodies_and_ages]
    return (0, json.dumps({"ticket_id": "abcd-1111-2222-3333", "comments": comments}), "")


def test_heartbeat_alert_opens_on_second_consecutive_red(mod: ModuleType, tmp_path: Path) -> None:
    """stale + no ticket + previous canary run also red -> rebar create bug (threshold met)."""
    runner = FakeRunner(
        {
            ("rebar", "list"): (0, "[]", ""),
            ("gh", "api"): prev_runs("failure"),
        }
    )
    rc = mod.main(["heartbeat-alert"], runner=runner, environ=alert_env(tmp_path), now_epoch=NOW)
    assert rc == 0
    creates = [c for c in runner.rebar_calls() if c[1] == "create"]
    assert len(creates) == 1
    argv = creates[0]
    assert argv[2] == "bug"
    assert argv[3] == (
        "[heartbeat-canary] reconcile-bridge stale (3h 5m ago) — no success within 2h"
    )
    assert "--priority" in argv and argv[argv.index("--priority") + 1] == "1"
    assert "--tags" in argv and argv[argv.index("--tags") + 1] == "heartbeat-alert"
    assert "--detected-by" in argv
    assert argv[argv.index("--detected-by") + 1] == "heartbeat-canary"
    assert "--description" in argv
    desc = argv[argv.index("--description") + 1]
    assert "reconcile-bridge-canary.yml" in desc  # workflow path in template
    assert _iso(NOW - 1200) in desc


def test_heartbeat_alert_first_red_files_nothing(mod: ModuleType, tmp_path: Path) -> None:
    """stale + no ticket + previous canary run green -> single flake, no ticket."""
    runner = FakeRunner(
        {
            ("rebar", "list"): (0, "[]", ""),
            ("gh", "api"): prev_runs("success"),
        }
    )
    rc = mod.main(["heartbeat-alert"], runner=runner, environ=alert_env(tmp_path), now_epoch=NOW)
    assert rc == 0
    assert runner.rebar_writes() == []


def test_heartbeat_alert_no_run_history_files_nothing(mod: ModuleType, tmp_path: Path) -> None:
    """stale + no ticket + no prior completed run -> treated as first red."""
    runner = FakeRunner(
        {
            ("rebar", "list"): (0, "[]", ""),
            ("gh", "api"): (0, "[]", ""),
        }
    )
    rc = mod.main(["heartbeat-alert"], runner=runner, environ=alert_env(tmp_path), now_epoch=NOW)
    assert rc == 0
    assert runner.rebar_writes() == []


def test_heartbeat_alert_history_query_failure_files_nothing(
    mod: ModuleType, tmp_path: Path
) -> None:
    """Run-history query failure fails toward NOT filing (loud log, exit 0)."""
    runner = FakeRunner(
        {
            ("rebar", "list"): (0, "[]", ""),
            ("gh", "api"): (1, "", "boom: rate limited"),
        }
    )
    rc = mod.main(["heartbeat-alert"], runner=runner, environ=alert_env(tmp_path), now_epoch=NOW)
    assert rc == 0
    assert runner.rebar_writes() == []


def test_heartbeat_alert_excludes_current_run_from_history(mod: ModuleType, tmp_path: Path) -> None:
    """The current run's own row never satisfies the threshold."""
    runs = [
        {"id": 42, "conclusion": "failure", "updated_at": _iso(NOW - 60)},  # current run
        {"id": 41, "conclusion": "success", "updated_at": _iso(NOW - 1200)},
    ]
    runner = FakeRunner(
        {
            ("rebar", "list"): (0, "[]", ""),
            ("gh", "api"): (0, json.dumps(runs), ""),
        }
    )
    rc = mod.main(["heartbeat-alert"], runner=runner, environ=alert_env(tmp_path), now_epoch=NOW)
    assert rc == 0
    assert runner.rebar_writes() == []


def test_heartbeat_alert_empty_detail_aborts_loud(mod: ModuleType, tmp_path: Path) -> None:
    """Empty STATUS_MSG -> abort with non-zero exit; no hollow ticket."""
    runner = FakeRunner(
        {
            ("rebar", "list"): (0, "[]", ""),
            ("gh", "api"): prev_runs("failure"),
        }
    )
    env = alert_env(tmp_path, STATUS_MSG="   ")
    rc = mod.main(["heartbeat-alert"], runner=runner, environ=env, now_epoch=NOW)
    assert rc != 0
    assert runner.rebar_writes() == []


def test_heartbeat_alert_comments_when_stale_and_open(mod: ModuleType, tmp_path: Path) -> None:
    """stale + existing alert -> BRIDGE_CANARY_ALERT-prefixed comment, no create.

    The dedup search runs FIRST: an open tagged ticket takes the accumulate path
    regardless of the consecutive-red threshold (no gh api call needed).
    """
    listing = json.dumps([{"ticket_id": "abcd-1111-2222-3333"}])
    runner = FakeRunner(
        {
            ("rebar", "list"): (0, listing, ""),
            ("rebar", "show"): show_with_comments(),
        }
    )
    rc = mod.main(["heartbeat-alert"], runner=runner, environ=alert_env(tmp_path), now_epoch=NOW)
    assert rc == 0
    writes = runner.rebar_writes()
    assert [c[1] for c in writes] == ["comment"]
    assert writes[0][2] == "abcd-1111-2222-3333"
    body = writes[0][3]
    assert body.startswith("BRIDGE_CANARY_ALERT: Still stale as of ")
    assert "Last successful run was 3h 5m ago" in body
    assert "https://github.com/navapbc/rebar/actions/runs/42" in body
    assert [c[:2] for c in runner.calls if c[0] == "gh"] == []


def test_heartbeat_alert_second_accumulation_within_24h_skipped(
    mod: ModuleType, tmp_path: Path
) -> None:
    """A marker comment younger than 24h suppresses another accumulation comment."""
    listing = json.dumps([{"ticket_id": "abcd-1111-2222-3333"}])
    runner = FakeRunner(
        {
            ("rebar", "list"): (0, listing, ""),
            ("rebar", "show"): show_with_comments(
                ("BRIDGE_CANARY_ALERT: Still stale as of earlier today", 3 * 3600)
            ),
        }
    )
    rc = mod.main(["heartbeat-alert"], runner=runner, environ=alert_env(tmp_path), now_epoch=NOW)
    assert rc == 0
    assert runner.rebar_writes() == []


def test_heartbeat_alert_accumulates_after_24h(mod: ModuleType, tmp_path: Path) -> None:
    """A marker comment older than 24h no longer suppresses accumulation."""
    listing = json.dumps([{"ticket_id": "abcd-1111-2222-3333"}])
    runner = FakeRunner(
        {
            ("rebar", "list"): (0, listing, ""),
            ("rebar", "show"): show_with_comments(
                ("BRIDGE_CANARY_ALERT: Still stale as of yesterday", 25 * 3600),
                ("an unrelated agent comment", 600),
            ),
        }
    )
    rc = mod.main(["heartbeat-alert"], runner=runner, environ=alert_env(tmp_path), now_epoch=NOW)
    assert rc == 0
    writes = runner.rebar_writes()
    assert [c[1] for c in writes] == ["comment"]


def test_heartbeat_alert_closes_on_recovery(mod: ModuleType, tmp_path: Path) -> None:
    """not-stale + existing alert -> transition open->closed with class + Fixed: reason."""
    listing = json.dumps([{"ticket_id": "abcd-1111-2222-3333"}])
    runner = FakeRunner({("rebar", "list"): (0, listing, "")})
    env = alert_env(
        tmp_path,
        STALE="false",
        STATUS_MSG="Reconciler healthy — last successful run was 0h 9m ago.",
    )
    rc = mod.main(["heartbeat-alert"], runner=runner, environ=env, now_epoch=NOW)
    assert rc == 0
    writes = runner.rebar_writes()
    assert [c[1] for c in writes] == ["transition"]
    argv = writes[0]
    assert argv[2:5] == ["abcd-1111-2222-3333", "open", "closed"]
    assert "--class" in argv and argv[argv.index("--class") + 1] == "env_integration"
    reason = argv[argv.index("--reason") + 1]
    assert reason.startswith("Fixed: reconciler recovered at ")
    assert "--force" not in argv
    assert not any(a.startswith("--force=") for a in argv)


def test_heartbeat_alert_dry_run_makes_zero_rebar_writes(mod: ModuleType, tmp_path: Path) -> None:
    """DRY_RUN=true short-circuits every rebar mutation (defense in depth)."""
    runner = FakeRunner()
    env = alert_env(tmp_path, DRY_RUN="true")
    rc = mod.main(["heartbeat-alert"], runner=runner, environ=env, now_epoch=NOW)
    assert rc == 0
    assert runner.rebar_writes() == []


def test_heartbeat_alert_green_with_no_ticket_is_noop(mod: ModuleType, tmp_path: Path) -> None:
    """Green with no open tagged ticket (flake that never crossed the threshold) -> no-op."""
    runner = FakeRunner({("rebar", "list"): (0, "[]", "")})
    env = alert_env(tmp_path, STALE="false", STATUS_MSG="Reconciler healthy.")
    rc = mod.main(["heartbeat-alert"], runner=runner, environ=env, now_epoch=NOW)
    assert rc == 0
    assert runner.rebar_writes() == []


def test_check_binding_drift_exit1_with_json_is_drift_data(mod: ModuleType, tmp_path: Path) -> None:
    """Exit 1 is finding data; drift and store-integrity counts are combined."""
    fsck = json.dumps(
        {
            "unknown_event_types": [],
            "binding_drift": {
                "would_terminal": ["a", "b"],
                "dangling": [],
                "local_gone": ["c"],
                "unbound_jira": [],
                "retired_overlap": [],
            },
            "store_integrity": [
                {
                    "kind": "forward_missing_reverse",
                    "local_id": "loc-1",
                    "jira_key": "REB-1",
                }
            ],
        }
    )
    runner = FakeRunner({("rebar", "bridge", "fsck"): (1, fsck, "")})
    env = {"GITHUB_OUTPUT": str(tmp_path / "gh_out")}
    (tmp_path / "gh_out").touch()
    rc = mod.main(["check-binding-drift"], runner=runner, environ=env, now_epoch=NOW)
    assert rc == 0  # exit 1 from fsck is a SIGNAL, not an error
    out = read_outputs(tmp_path / "gh_out")
    assert out["drift_found"] == "true"
    assert out["drift_total"] == "4"
    assert out["drift_summary"] == "would_terminal=2, local_gone=1, store_integrity=1"


def test_check_binding_drift_clean(mod: ModuleType, tmp_path: Path) -> None:
    """Exit 0 + empty binding_drift -> drift_found=false, summary 'none'."""
    fsck = json.dumps({"unknown_event_types": [], "binding_drift": {}, "store_integrity": []})
    runner = FakeRunner({("rebar", "bridge", "fsck"): (0, fsck, "")})
    env = {"GITHUB_OUTPUT": str(tmp_path / "gh_out")}
    (tmp_path / "gh_out").touch()
    rc = mod.main(["check-binding-drift"], runner=runner, environ=env, now_epoch=NOW)
    assert rc == 0
    out = read_outputs(tmp_path / "gh_out")
    assert out["drift_found"] == "false"
    assert out["drift_total"] == "0"
    assert out["drift_summary"] == "none"


def test_check_binding_drift_garbage_output_fails_closed(mod: ModuleType, tmp_path: Path) -> None:
    """Unparseable fsck stdout cannot be reinterpreted as a clean store."""
    runner = FakeRunner({("rebar", "bridge", "fsck"): (1, "not json {", "")})
    env = {"GITHUB_OUTPUT": str(tmp_path / "gh_out")}
    (tmp_path / "gh_out").touch()
    rc = mod.main(["check-binding-drift"], runner=runner, environ=env, now_epoch=NOW)
    assert rc == 1
    assert (tmp_path / "gh_out").read_text() == ""


def test_check_binding_drift_operational_exit_two_fails_closed(
    mod: ModuleType, tmp_path: Path
) -> None:
    """A Git/ref scan failure makes the canary step fail, never auto-close."""
    runner = FakeRunner({("rebar", "bridge", "fsck"): (2, "", "tickets ref unavailable")})
    env = {"GITHUB_OUTPUT": str(tmp_path / "gh_out")}
    (tmp_path / "gh_out").touch()

    rc = mod.main(["check-binding-drift"], runner=runner, environ=env, now_epoch=NOW)

    assert rc == 1
    assert (tmp_path / "gh_out").read_text() == ""


def test_check_binding_drift_partial_old_shape_fails_closed(
    mod: ModuleType, tmp_path: Path
) -> None:
    """An old partial payload cannot silently bypass store-integrity findings."""
    runner = FakeRunner({("rebar", "bridge", "fsck"): (0, json.dumps({"binding_drift": {}}), "")})
    env = {"GITHUB_OUTPUT": str(tmp_path / "gh_out")}
    (tmp_path / "gh_out").touch()

    rc = mod.main(["check-binding-drift"], runner=runner, environ=env, now_epoch=NOW)

    assert rc == 1
    assert (tmp_path / "gh_out").read_text() == ""


def drift_env(tmp_path: Path, **over: str) -> dict[str, str]:
    env = {
        "DRY_RUN": "false",
        "DRIFT_FOUND": "true",
        "DRIFT_TOTAL": "3",
        "DRIFT_SUMMARY": "would_terminal=2, local_gone=1",
        "RUN_URL": "https://github.com/navapbc/rebar/actions/runs/42",
    }
    env.update(over)
    return env


def test_binding_drift_alert_opens_with_drift_title(mod: ModuleType, tmp_path: Path) -> None:
    """First observed drift files immediately — NO consecutive-red threshold: drift is
    persistent state (it cannot self-heal between runs) and the fsck oracle never fails
    the canary run, so run conclusions carry no drift signal to count."""
    runner = FakeRunner({("rebar", "list"): (0, "[]", "")})
    rc = mod.main(
        ["binding-drift-alert"], runner=runner, environ=drift_env(tmp_path), now_epoch=NOW
    )
    assert rc == 0
    creates = [c for c in runner.rebar_calls() if c[1] == "create"]
    assert len(creates) == 1
    argv = creates[0]
    assert argv[3] == "[binding-drift] bridge fsck found 3 audit finding(s)"
    assert "--tags" in argv and argv[argv.index("--tags") + 1] == "binding-drift-alert"
    assert "--detected-by" in argv
    assert argv[argv.index("--detected-by") + 1] == "binding-drift-canary"
    # dedup find used the drift tag, not the heartbeat tag
    finds = [c for c in runner.rebar_calls() if c[1] == "list"]
    assert finds and any("--has-tag=binding-drift-alert" in a for a in finds[0])
    # no run-history threshold query for drift
    assert [c for c in runner.calls if c[0] == "gh"] == []


def test_binding_drift_alert_accumulation_within_24h_skipped(
    mod: ModuleType, tmp_path: Path
) -> None:
    """Repeat drift with a <24h-old marker comment -> no duplicate accumulation."""
    listing = json.dumps([{"ticket_id": "abcd-1111-2222-3333"}])
    runner = FakeRunner(
        {
            ("rebar", "list"): (0, listing, ""),
            ("rebar", "show"): show_with_comments(
                ("BRIDGE_CANARY_ALERT: Binding drift still present as of earlier", 2 * 3600)
            ),
        }
    )
    rc = mod.main(
        ["binding-drift-alert"], runner=runner, environ=drift_env(tmp_path), now_epoch=NOW
    )
    assert rc == 0
    assert runner.rebar_writes() == []


def test_binding_drift_alert_accumulates_after_24h(mod: ModuleType, tmp_path: Path) -> None:
    listing = json.dumps([{"ticket_id": "abcd-1111-2222-3333"}])
    runner = FakeRunner(
        {
            ("rebar", "list"): (0, listing, ""),
            ("rebar", "show"): show_with_comments(
                ("BRIDGE_CANARY_ALERT: Binding drift still present as of yesterday", 26 * 3600)
            ),
        }
    )
    rc = mod.main(
        ["binding-drift-alert"], runner=runner, environ=drift_env(tmp_path), now_epoch=NOW
    )
    assert rc == 0
    writes = runner.rebar_writes()
    assert [c[1] for c in writes] == ["comment"]
    assert writes[0][3].startswith(
        "BRIDGE_CANARY_ALERT: Bridge audit findings still present as of "
    )


def test_binding_drift_alert_empty_detail_aborts_loud(mod: ModuleType, tmp_path: Path) -> None:
    """drift_found=true with an empty/'none' summary is contradictory -> abort, no ticket."""
    runner = FakeRunner({("rebar", "list"): (0, "[]", "")})
    env = drift_env(tmp_path, DRIFT_SUMMARY="none")
    rc = mod.main(["binding-drift-alert"], runner=runner, environ=env, now_epoch=NOW)
    assert rc != 0
    assert runner.rebar_writes() == []


def test_binding_drift_alert_green_with_no_ticket_is_noop(mod: ModuleType, tmp_path: Path) -> None:
    runner = FakeRunner({("rebar", "list"): (0, "[]", "")})
    env = drift_env(tmp_path, DRIFT_FOUND="false", DRIFT_TOTAL="0", DRIFT_SUMMARY="none")
    rc = mod.main(["binding-drift-alert"], runner=runner, environ=env, now_epoch=NOW)
    assert rc == 0
    assert runner.rebar_writes() == []


def test_binding_drift_alert_write_failure_fails_loud(mod: ModuleType, tmp_path: Path) -> None:
    """A failing rebar WRITE must surface as a non-zero exit (red canary run)."""
    runner = FakeRunner(
        {
            ("rebar", "list"): (0, "[]", ""),
            ("rebar", "create"): (1, "", "store locked"),
        }
    )
    rc = mod.main(
        ["binding-drift-alert"], runner=runner, environ=drift_env(tmp_path), now_epoch=NOW
    )
    assert rc != 0


# ── held-out oracles (merged from test_canary_bridge_heldout) ──


@pytest.fixture(scope="module")
def mod_heldout() -> ModuleType:
    spec = importlib.util.spec_from_file_location("canary_bridge_heldout", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeRunner_heldout:
    def __init__(self, responses: dict[tuple[str, ...], tuple[int, str, str]] | None = None):
        self.calls: list[list[str]] = []
        self.responses = responses or {}

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        self.calls.append(list(argv))
        best: tuple[str, ...] | None = None
        for prefix in self.responses:
            if tuple(argv[: len(prefix)]) == prefix and (best is None or len(prefix) > len(best)):
                best = prefix
        return self.responses[best] if best is not None else (0, "", "")

    def rebar_writes(self) -> list[list[str]]:
        return [
            c
            for c in self.calls
            if c and c[0] == "rebar" and c[1] in ("create", "comment", "transition")
        ]


def alert_env_heldout(tmp_path: Path, **over: str) -> dict[str, str]:
    env = {
        "DRY_RUN": "false",
        "ALERT_TAG": "heartbeat-alert",
        "ALERT_WINDOW_HOURS": "2",
        "STALE": "true",
        "LAST_RUN_AGO": "3h 5m ago",
        "STATUS_MSG": "Last successful run was 3h 5m ago (threshold: 2h).",
        "RUN_URL": "https://github.com/navapbc/rebar/actions/runs/42",
    }
    env.update(over)
    return env


class SequenceRunner(FakeRunner_heldout):
    """Return successive responses for repeated canonical status snapshots."""

    def __init__(self, responses: list[tuple[int, str, str]]):
        super().__init__()
        self._sequence = iter(responses)

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        self.calls.append(list(argv))
        if argv[:3] == ["rebar", "bridge", "status"]:
            return next(self._sequence)
        return (0, "", "")


def status_env(tmp_path: Path, **over: str) -> dict[str, str]:
    env = hb_env(tmp_path)
    env.update(over)
    return env


def _status(verdict: str, **extra: object) -> tuple[int, str, str]:
    payload: dict[str, object] = {"verdict": verdict, "pass_id": "pass-canary"}
    payload.update(extra)
    lock = payload.get("lock")
    if isinstance(lock, dict):
        payload.update(
            lock_oid=lock.get("oid"),
            live_lock_fence=lock.get("fence"),
            lock_lease_secs=lock.get("lease_secs"),
        )
    return (0 if verdict in {"HEALTHY", "PAUSED", "RUNNING"} else 1, json.dumps(payload), "")


def test_canonical_status_is_unconditional_and_never_queries_workflow_heartbeat(
    mod_heldout: ModuleType, tmp_path: Path
) -> None:
    runner = SequenceRunner([_status("HEALTHY")])
    env = hb_env(tmp_path)
    rc = mod_heldout.main(["check-heartbeat"], runner=runner, environ=env, now_epoch=NOW)
    assert rc == 0
    assert read_outputs(tmp_path / "gh_out")["stale"] == "false"
    assert runner.calls == [
        [
            "rebar",
            "bridge",
            "status",
            "--target",
            "reconciler",
            "--max-age",
            "2h",
            "--json",
        ]
    ]


def test_running_snapshot_waits_observed_lease_and_advancing_fence_stays_running(
    mod_heldout: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(mod_heldout.time, "sleep", sleeps.append)
    runner = SequenceRunner(
        [
            _status(
                "RUNNING",
                lock={"oid": "a" * 40, "fence": 8, "lease_secs": 120, "holder": "p"},
            ),
            _status(
                "RUNNING",
                lock={"oid": "b" * 40, "fence": 9, "lease_secs": 120, "holder": "p"},
            ),
        ]
    )
    rc = mod_heldout.main(
        ["check-heartbeat"], runner=runner, environ=status_env(tmp_path), now_epoch=NOW
    )
    assert rc == 0
    assert sleeps == [120]
    out = read_outputs(tmp_path / "gh_out")
    assert out["stale"] == "false"
    assert "advanced" in out["status_msg"].lower()


def test_stalled_fence_across_lease_is_crashed(
    mod_heldout: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mod_heldout.time, "sleep", lambda _seconds: None)
    lock = {"oid": "a" * 40, "fence": 8, "lease_secs": 120, "holder": "p"}
    runner = SequenceRunner([_status("RUNNING", lock=lock), _status("RUNNING", lock=lock)])
    rc = mod_heldout.main(
        ["check-heartbeat"], runner=runner, environ=status_env(tmp_path), now_epoch=NOW
    )
    assert rc == 0
    out = read_outputs(tmp_path / "gh_out")
    assert out["stale"] == "true"
    assert "crashed" in out["status_msg"].lower()


def test_lease_over_job_budget_fails_loudly_without_sleep(
    mod_heldout: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        mod_heldout.time,
        "sleep",
        lambda _seconds: pytest.fail("oversized lease must fail before sleeping"),
    )
    runner = SequenceRunner(
        [
            _status(
                "RUNNING",
                lock={"oid": "a" * 40, "fence": 8, "lease_secs": 481, "holder": "p"},
            )
        ]
    )
    rc = mod_heldout.main(
        ["check-heartbeat"], runner=runner, environ=status_env(tmp_path), now_epoch=NOW
    )
    assert rc == 1
    assert "480" in capsys.readouterr().out


THRESHOLD_ENV = {
    "GITHUB_REPOSITORY": "navapbc/rebar",
    "GITHUB_RUN_ID": "42",
    "CANARY_WORKFLOW_FILE": "reconcile-bridge-canary.yml",
}


def prev_red() -> tuple[int, str, str]:
    runs = [{"id": 41, "conclusion": "failure", "updated_at": _iso(NOW - 1200)}]
    return (0, json.dumps(runs), "")


def test_exact_cutoff_boundary_is_not_stale(mod_heldout: ModuleType, tmp_path: Path) -> None:
    """YAML: stale iff run_epoch < cutoff_epoch (STRICT). run_epoch == cutoff -> fresh."""
    run_epoch = NOW - 2 * 3600  # exactly the 2h window
    payload = json.dumps({"updated_at": _iso(run_epoch)})
    runner = FakeRunner_heldout({("gh", "api"): (0, payload, ""), **NEVER_RUN})
    rc = mod_heldout.main(
        ["check-heartbeat"], runner=runner, environ=hb_env(tmp_path), now_epoch=NOW
    )
    assert rc == 0
    assert read_outputs(tmp_path / "gh_out")["stale"] == "false"


def test_one_second_past_cutoff_is_stale(mod_heldout: ModuleType, tmp_path: Path) -> None:
    run_epoch = NOW - 2 * 3600 - 1
    payload = json.dumps({"updated_at": _iso(run_epoch)})
    runner = FakeRunner_heldout({("gh", "api"): (0, payload, ""), **NEVER_RUN})
    rc = mod_heldout.main(
        ["check-heartbeat"], runner=runner, environ=hb_env(tmp_path), now_epoch=NOW
    )
    assert rc == 0
    assert read_outputs(tmp_path / "gh_out")["stale"] == "true"


def test_age_arithmetic_truncates_like_shell(mod_heldout: ModuleType, tmp_path: Path) -> None:
    """7385s = 2h 3m (integer division twice, seconds dropped)."""
    run_epoch = NOW - 7385
    payload = json.dumps({"updated_at": _iso(run_epoch)})
    runner = FakeRunner_heldout({("gh", "api"): (0, payload, ""), **NEVER_RUN})
    rc = mod_heldout.main(
        ["check-heartbeat"], runner=runner, environ=hb_env(tmp_path), now_epoch=NOW
    )
    assert rc == 0
    assert read_outputs(tmp_path / "gh_out")["last_run_ago"] == "2h 3m ago"


@pytest.mark.parametrize("bad", ["0", "-2", "1.5", "", "2h"])
def test_invalid_windows_rejected(mod_heldout: ModuleType, tmp_path: Path, bad: str) -> None:
    runner = FakeRunner_heldout()
    rc = mod_heldout.main(
        ["check-heartbeat"],
        runner=runner,
        environ=hb_env(tmp_path, ALERT_WINDOW_HOURS=bad),
        now_epoch=NOW,
    )
    assert rc == 1
    assert runner.calls == []


def test_large_window_accepted(mod_heldout: ModuleType, tmp_path: Path) -> None:
    run_epoch = NOW - 20 * 3600
    payload = json.dumps({"updated_at": _iso(run_epoch)})
    runner = FakeRunner_heldout({("gh", "api"): (0, payload, ""), **NEVER_RUN})
    rc = mod_heldout.main(
        ["check-heartbeat"],
        runner=runner,
        environ=hb_env(tmp_path, ALERT_WINDOW_HOURS="24"),
        now_epoch=NOW,
    )
    assert rc == 0
    out = read_outputs(tmp_path / "gh_out")
    assert out["stale"] == "false"
    assert out["status_msg"].startswith("Reconciler healthy")


def test_github_output_appends_not_truncates(mod_heldout: ModuleType, tmp_path: Path) -> None:
    """$GITHUB_OUTPUT is append-mode: earlier keys written by other steps survive."""
    env = hb_env(tmp_path)
    Path(env["GITHUB_OUTPUT"]).write_text("earlier=kept\n", encoding="utf-8")
    runner = FakeRunner_heldout({("gh", "api"): (0, "", ""), **NEVER_RUN})
    rc = mod_heldout.main(["check-heartbeat"], runner=runner, environ=env, now_epoch=NOW)
    assert rc == 0
    out = read_outputs(tmp_path / "gh_out")
    assert out["earlier"] == "kept"
    assert out["stale"] == "true"


def test_find_failure_is_soft_open_proceeds(mod_heldout: ModuleType, tmp_path: Path) -> None:
    """rebar list failing -> treated as 'no existing alert' (YAML `|| echo ''`)."""
    runner = FakeRunner_heldout(
        {
            ("rebar", "list"): (1, "", "store busy"),
            ("rebar", "create"): (0, "", ""),
            ("gh", "api"): prev_red(),
        }
    )
    env = alert_env_heldout(tmp_path, **THRESHOLD_ENV)
    rc = mod_heldout.main(["heartbeat-alert"], runner=runner, environ=env, now_epoch=NOW)
    assert rc == 0
    assert [c[1] for c in runner.rebar_writes()] == ["create"]


def test_find_garbage_json_is_soft(mod_heldout: ModuleType, tmp_path: Path) -> None:
    runner = FakeRunner_heldout(
        {
            ("rebar", "list"): (0, "certainly not json", ""),
            ("rebar", "create"): (0, "", ""),
            ("gh", "api"): prev_red(),
        }
    )
    env = alert_env_heldout(tmp_path, **THRESHOLD_ENV)
    rc = mod_heldout.main(["heartbeat-alert"], runner=runner, environ=env, now_epoch=NOW)
    assert rc == 0
    assert [c[1] for c in runner.rebar_writes()] == ["create"]


def test_find_picks_first_ticket(mod_heldout: ModuleType, tmp_path: Path) -> None:
    listing = json.dumps([{"ticket_id": "aaaa-1"}, {"ticket_id": "bbbb-2"}])
    runner = FakeRunner_heldout({("rebar", "list"): (0, listing, "")})
    rc = mod_heldout.main(
        ["heartbeat-alert"], runner=runner, environ=alert_env_heldout(tmp_path), now_epoch=NOW
    )
    assert rc == 0
    writes = runner.rebar_writes()
    assert writes and writes[0][1] == "comment" and writes[0][2] == "aaaa-1"


def test_no_op_when_fresh_and_none_open(mod_heldout: ModuleType, tmp_path: Path) -> None:
    runner = FakeRunner_heldout({("rebar", "list"): (0, "[]", "")})
    env = alert_env_heldout(tmp_path, STALE="false")
    rc = mod_heldout.main(["heartbeat-alert"], runner=runner, environ=env, now_epoch=NOW)
    assert rc == 0
    assert runner.rebar_writes() == []


@pytest.mark.parametrize("verb", ["create", "comment", "transition"])
def test_each_write_failure_fails_loud(mod_heldout: ModuleType, tmp_path: Path, verb: str) -> None:
    if verb == "create":
        listing, stale = "[]", "true"
    elif verb == "comment":
        listing, stale = json.dumps([{"ticket_id": "x-1"}]), "true"
    else:
        listing, stale = json.dumps([{"ticket_id": "x-1"}]), "false"
    runner = FakeRunner_heldout(
        {
            ("rebar", "list"): (0, listing, ""),
            ("rebar", verb): (1, "", "kaboom"),
            ("gh", "api"): prev_red(),
        }
    )
    env = alert_env_heldout(tmp_path, STALE=stale, **THRESHOLD_ENV)
    rc = mod_heldout.main(["heartbeat-alert"], runner=runner, environ=env, now_epoch=NOW)
    assert rc != 0


def test_close_argv_exact_shape(mod_heldout: ModuleType, tmp_path: Path) -> None:
    listing = json.dumps([{"ticket_id": "x-1"}])
    runner = FakeRunner_heldout({("rebar", "list"): (0, listing, "")})
    env = alert_env_heldout(tmp_path, STALE="false", STATUS_MSG="Reconciler healthy — ok.")
    rc = mod_heldout.main(["heartbeat-alert"], runner=runner, environ=env, now_epoch=NOW)
    assert rc == 0
    argv = runner.rebar_writes()[0]
    assert argv[0:5] == ["rebar", "transition", "x-1", "open", "closed"]
    i = argv.index("--class")
    assert argv[i + 1] == "env_integration"
    reason = argv[argv.index("--reason") + 1]
    assert reason.startswith("Fixed: ")
    force = [a for a in argv if a.startswith("--force=")]
    assert force == []


def test_dry_run_skips_even_the_find(mod_heldout: ModuleType, tmp_path: Path) -> None:
    """DRY_RUN=true -> zero rebar WRITES (find may or may not run; writes must not)."""
    runner = FakeRunner_heldout({("rebar", "list"): (0, json.dumps([{"ticket_id": "x"}]), "")})
    env = alert_env_heldout(tmp_path, DRY_RUN="true", STALE="false")
    rc = mod_heldout.main(["heartbeat-alert"], runner=runner, environ=env, now_epoch=NOW)
    assert rc == 0
    assert runner.rebar_writes() == []


def test_comment_timestamp_derived_from_now_epoch(mod_heldout: ModuleType, tmp_path: Path) -> None:
    listing = json.dumps([{"ticket_id": "x-1"}])
    runner = FakeRunner_heldout({("rebar", "list"): (0, listing, "")})
    rc = mod_heldout.main(
        ["heartbeat-alert"], runner=runner, environ=alert_env_heldout(tmp_path), now_epoch=NOW
    )
    assert rc == 0
    body = runner.rebar_writes()[0][3]
    assert _iso(NOW) in body


def test_fsck_empty_stdout_fails_closed(mod_heldout: ModuleType, tmp_path: Path) -> None:
    runner = FakeRunner_heldout({("rebar", "bridge", "fsck"): (1, "", "")})
    env = {"GITHUB_OUTPUT": str(tmp_path / "gh_out")}
    (tmp_path / "gh_out").touch()
    rc = mod_heldout.main(["check-binding-drift"], runner=runner, environ=env, now_epoch=NOW)
    assert rc == 1
    assert (tmp_path / "gh_out").read_text() == ""


def test_fsck_missing_binding_drift_key(mod_heldout: ModuleType, tmp_path: Path) -> None:
    runner = FakeRunner_heldout({("rebar", "bridge", "fsck"): (0, json.dumps({"other": 1}), "")})
    env = {"GITHUB_OUTPUT": str(tmp_path / "gh_out")}
    (tmp_path / "gh_out").touch()
    rc = mod_heldout.main(["check-binding-drift"], runner=runner, environ=env, now_epoch=NOW)
    assert rc == 1
    assert (tmp_path / "gh_out").read_text() == ""


def test_summary_cell_order_is_canonical(mod_heldout: ModuleType, tmp_path: Path) -> None:
    """Summary lists only alerting cells in their fixed canonical order."""
    fsck = json.dumps(
        {
            "unknown_event_types": [],
            "binding_drift": {
                "retired_overlap": ["r"],
                "unbound_jira": ["u1", "u2"],
                "would_terminal": [],
                "dangling": ["d"],
                "local_gone": [],
            },
            "store_integrity": [],
        }
    )
    runner = FakeRunner_heldout({("rebar", "bridge", "fsck"): (1, fsck, "")})
    env = {"GITHUB_OUTPUT": str(tmp_path / "gh_out")}
    (tmp_path / "gh_out").touch()
    rc = mod_heldout.main(["check-binding-drift"], runner=runner, environ=env, now_epoch=NOW)
    assert rc == 0
    out = read_outputs(tmp_path / "gh_out")
    assert out["drift_summary"] == "unbound_jira=2, retired_overlap=1"
    assert out["drift_total"] == "3"


def test_unknown_extra_cells_ignored(mod_heldout: ModuleType, tmp_path: Path) -> None:
    """Informational and unknown cells do not open an alert ticket."""
    fsck = json.dumps(
        {
            "unknown_event_types": [],
            "binding_drift": {"mystery": ["m"], "dangling": ["d"]},
            "store_integrity": [],
        }
    )
    runner = FakeRunner_heldout({("rebar", "bridge", "fsck"): (1, fsck, "")})
    env = {"GITHUB_OUTPUT": str(tmp_path / "gh_out")}
    (tmp_path / "gh_out").touch()
    rc = mod_heldout.main(["check-binding-drift"], runner=runner, environ=env, now_epoch=NOW)
    assert rc == 0
    out = read_outputs(tmp_path / "gh_out")
    assert out["drift_found"] == "false"
    assert out["drift_total"] == "0"
    assert out["drift_summary"] == "none"


def test_drift_alert_comment_format(mod_heldout: ModuleType, tmp_path: Path) -> None:
    listing = json.dumps([{"ticket_id": "dd-1"}])
    runner = FakeRunner_heldout({("rebar", "list"): (0, listing, "")})
    rc = mod_heldout.main(
        ["binding-drift-alert"], runner=runner, environ=drift_env(tmp_path), now_epoch=NOW
    )
    assert rc == 0
    writes = runner.rebar_writes()
    assert [c[1] for c in writes] == ["comment"]
    body = writes[0][3]
    assert body.startswith("BRIDGE_CANARY_ALERT: Bridge audit findings still present as of ")
    assert "would_terminal=2, local_gone=1" in body


def test_drift_alert_close_on_recovery(mod_heldout: ModuleType, tmp_path: Path) -> None:
    listing = json.dumps([{"ticket_id": "dd-1"}])
    runner = FakeRunner_heldout({("rebar", "list"): (0, listing, "")})
    env = drift_env(tmp_path, DRIFT_FOUND="false")
    rc = mod_heldout.main(["binding-drift-alert"], runner=runner, environ=env, now_epoch=NOW)
    assert rc == 0
    argv = runner.rebar_writes()[0]
    assert argv[1] == "transition"
    assert "--class" in argv and argv[argv.index("--class") + 1] == "env_integration"
    reason = argv[argv.index("--reason") + 1]
    assert reason.startswith("Fixed: bridge fsck reports zero audit findings")
    assert "--force" not in argv
    assert not any(a.startswith("--force=") for a in argv)


def test_drift_alert_dry_run_zero_writes(mod_heldout: ModuleType, tmp_path: Path) -> None:
    runner = FakeRunner_heldout({("rebar", "list"): (0, json.dumps([{"ticket_id": "dd-1"}]), "")})
    env = drift_env(tmp_path, DRY_RUN="true")
    rc = mod_heldout.main(["binding-drift-alert"], runner=runner, environ=env, now_epoch=NOW)
    assert rc == 0
    assert runner.rebar_writes() == []


def test_drift_alert_noop_when_clean_and_none(mod_heldout: ModuleType, tmp_path: Path) -> None:
    runner = FakeRunner_heldout({("rebar", "list"): (0, "[]", "")})
    env = drift_env(tmp_path, DRIFT_FOUND="false")
    rc = mod_heldout.main(["binding-drift-alert"], runner=runner, environ=env, now_epoch=NOW)
    assert rc == 0
    assert runner.rebar_writes() == []
