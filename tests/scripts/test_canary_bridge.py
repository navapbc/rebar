"""Oracle suite for scripts/canary_bridge.py (ticket e602-1354-6778-4c0f).

The reconcile-bridge-canary workflow's classification + alert-lifecycle logic
migrates out of YAML run-blocks into ``scripts/canary_bridge.py`` (Tier 3 of
the shell->Python strangler-fig). This suite pins the module contract:

Subcommands (argv[0]):
- ``check-heartbeat``    — classify reconcile-bridge.yml staleness from the
  GitHub Actions API (via the injected runner calling ``gh api``); emit
  ``stale`` / ``last_run_ago`` / ``status_msg`` to $GITHUB_OUTPUT.
- ``heartbeat-alert``    — find/open/comment/close the ``heartbeat-alert``
  bug-ticket lifecycle against the rebar CLI (via the runner).
- ``check-binding-drift`` — run ``rebar bridge-fsck --output json``, tolerate
  its designed exit-1-on-drift, classify the ``binding_drift`` section; emit
  ``drift_found`` / ``drift_total`` / ``drift_summary`` to $GITHUB_OUTPUT.
- ``binding-drift-alert`` — same lifecycle shape for ``binding-drift-alert``.

Seams (keyword-only params of ``main``):
- ``runner(argv) -> (returncode, stdout, stderr)`` — every external command
  (``gh``, ``rebar``) goes through it; unit tests inject a fake.
- ``environ`` — mapping read instead of os.environ.
- ``now_epoch`` — int unix time used for age math and timestamps.

Failure dispositions (each preserved from the YAML, NOT a blanket rule):
- alert-lifecycle WRITES (create/comment/transition) fail LOUD -> exit != 0;
- dedup FINDs (``rebar list``) fail SOFT -> treated as "no existing alert";
- ``bridge-fsck`` exit 1 with valid JSON is DRIFT DATA (the signal), never an
  error; unparseable/empty stdout degrades to empty drift data.
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


NOW = 1_785_800_000  # arbitrary fixed epoch


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


# ---------------------------------------------------------------------------
# check-heartbeat
# ---------------------------------------------------------------------------


def test_check_heartbeat_fresh(mod: ModuleType, tmp_path: Path) -> None:
    """A success run inside the window -> stale=false + healthy message."""
    run_epoch = NOW - 3600 - 120  # 1h 2m ago, window 2h
    payload = json.dumps({"updated_at": _iso(run_epoch)})
    runner = FakeRunner({("gh", "api"): (0, payload, "")})
    env = hb_env(tmp_path)
    rc = mod.main(["check-heartbeat"], runner=runner, environ=env, now_epoch=NOW)
    assert rc == 0
    out = read_outputs(tmp_path / "gh_out")
    assert out["stale"] == "false"
    assert out["last_run_ago"] == "1h 2m ago"
    assert out["status_msg"] == "Reconciler healthy — last successful run was 1h 2m ago."
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
    runner = FakeRunner({("gh", "api"): (0, payload, "")})
    env = hb_env(tmp_path)
    rc = mod.main(["check-heartbeat"], runner=runner, environ=env, now_epoch=NOW)
    assert rc == 0
    out = read_outputs(tmp_path / "gh_out")
    assert out["stale"] == "true"
    assert out["last_run_ago"] == "3h 5m ago"
    assert out["status_msg"] == "Last successful run was 3h 5m ago (threshold: 2h)."


def test_check_heartbeat_no_success_runs(mod: ModuleType, tmp_path: Path) -> None:
    """Empty API result (legit zero-state) -> stale=true, last_run_ago=never."""
    runner = FakeRunner({("gh", "api"): (0, "", "")})
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
    runner = FakeRunner({("gh", "api"): (1, "", "boom: rate limited")})
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
    assert runner.calls == []  # fails before any API call


# ---------------------------------------------------------------------------
# heartbeat-alert lifecycle
# ---------------------------------------------------------------------------


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
    assert _iso(NOW - 1200) in desc  # first-red time = previous red run's timestamp


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
    assert [c[:2] for c in runner.calls if c[0] == "gh"] == []  # dedup short-circuits threshold


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
    assert any(a.startswith("--force-close=") for a in argv)


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


# ---------------------------------------------------------------------------
# check-binding-drift
# ---------------------------------------------------------------------------


def test_check_binding_drift_exit1_with_json_is_drift_data(mod: ModuleType, tmp_path: Path) -> None:
    """bridge-fsck exits 1 BY DESIGN on drift: capture stdout, report the drift."""
    fsck = json.dumps(
        {
            "binding_drift": {
                "would_terminal": ["a", "b"],
                "dangling": [],
                "local_gone": ["c"],
                "unbound_jira": [],
                "retired_overlap": [],
            }
        }
    )
    runner = FakeRunner({("rebar", "bridge-fsck"): (1, fsck, "")})
    env = {"GITHUB_OUTPUT": str(tmp_path / "gh_out")}
    (tmp_path / "gh_out").touch()
    rc = mod.main(["check-binding-drift"], runner=runner, environ=env, now_epoch=NOW)
    assert rc == 0  # exit 1 from fsck is a SIGNAL, not an error
    out = read_outputs(tmp_path / "gh_out")
    assert out["drift_found"] == "true"
    assert out["drift_total"] == "3"
    assert out["drift_summary"] == "would_terminal=2, local_gone=1"


def test_check_binding_drift_clean(mod: ModuleType, tmp_path: Path) -> None:
    """Exit 0 + empty binding_drift -> drift_found=false, summary 'none'."""
    fsck = json.dumps({"binding_drift": {}})
    runner = FakeRunner({("rebar", "bridge-fsck"): (0, fsck, "")})
    env = {"GITHUB_OUTPUT": str(tmp_path / "gh_out")}
    (tmp_path / "gh_out").touch()
    rc = mod.main(["check-binding-drift"], runner=runner, environ=env, now_epoch=NOW)
    assert rc == 0
    out = read_outputs(tmp_path / "gh_out")
    assert out["drift_found"] == "false"
    assert out["drift_total"] == "0"
    assert out["drift_summary"] == "none"


def test_check_binding_drift_garbage_output_degrades_to_empty(
    mod: ModuleType, tmp_path: Path
) -> None:
    """Unparseable fsck stdout degrades to empty drift data (YAML parity)."""
    runner = FakeRunner({("rebar", "bridge-fsck"): (1, "not json {", "")})
    env = {"GITHUB_OUTPUT": str(tmp_path / "gh_out")}
    (tmp_path / "gh_out").touch()
    rc = mod.main(["check-binding-drift"], runner=runner, environ=env, now_epoch=NOW)
    assert rc == 0
    out = read_outputs(tmp_path / "gh_out")
    assert out["drift_found"] == "false"
    assert out["drift_total"] == "0"


# ---------------------------------------------------------------------------
# binding-drift-alert lifecycle
# ---------------------------------------------------------------------------


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
    assert argv[3] == "[binding-drift] bridge-fsck found 3 unhealed binding drift(s)"
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
    assert writes[0][3].startswith("BRIDGE_CANARY_ALERT: Binding drift still present as of ")


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
