"""HELD-OUT suite for scripts/canary_bridge.py (ticket e602) — not shown to
the implementer. Restored into tests/scripts/ before validation.

Covers boundary math, disposition edges, argv exactness, and GITHUB_OUTPUT
hygiene that the oracle deliberately leaves unstated.
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
    spec = importlib.util.spec_from_file_location("canary_bridge_heldout", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeRunner:
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


def read_outputs(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            out[k] = v
    return out


NOW = 1_785_800_000


def _iso(epoch: int) -> str:
    import datetime as dt

    return dt.datetime.fromtimestamp(epoch, tz=dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hb_env(tmp_path: Path, **over: str) -> dict[str, str]:
    env = {
        "ALERT_WINDOW_HOURS": "2",
        "GITHUB_REPOSITORY": "navapbc/rebar",
        "GITHUB_OUTPUT": str(tmp_path / "gh_out"),
    }
    env.update(over)
    (tmp_path / "gh_out").touch()
    return env


def alert_env(tmp_path: Path, **over: str) -> dict[str, str]:
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


# Consecutive-red threshold fixture (ticket 4527): the heartbeat filer only
# CREATES a ticket when the previous completed canary run was also red, so
# create-path tests must supply the run-history env + a prior-red response.
THRESHOLD_ENV = {
    "GITHUB_REPOSITORY": "navapbc/rebar",
    "GITHUB_RUN_ID": "42",
    "CANARY_WORKFLOW_FILE": "reconcile-bridge-canary.yml",
}


def prev_red() -> tuple[int, str, str]:
    runs = [{"id": 41, "conclusion": "failure", "updated_at": _iso(NOW - 1200)}]
    return (0, json.dumps(runs), "")


# --------------------------------------------------------------------------
# check-heartbeat boundaries and math
# --------------------------------------------------------------------------


def test_exact_cutoff_boundary_is_not_stale(mod: ModuleType, tmp_path: Path) -> None:
    """YAML: stale iff run_epoch < cutoff_epoch (STRICT). run_epoch == cutoff -> fresh."""
    run_epoch = NOW - 2 * 3600  # exactly the 2h window
    payload = json.dumps({"updated_at": _iso(run_epoch)})
    runner = FakeRunner({("gh", "api"): (0, payload, "")})
    rc = mod.main(["check-heartbeat"], runner=runner, environ=hb_env(tmp_path), now_epoch=NOW)
    assert rc == 0
    assert read_outputs(tmp_path / "gh_out")["stale"] == "false"


def test_one_second_past_cutoff_is_stale(mod: ModuleType, tmp_path: Path) -> None:
    run_epoch = NOW - 2 * 3600 - 1
    payload = json.dumps({"updated_at": _iso(run_epoch)})
    runner = FakeRunner({("gh", "api"): (0, payload, "")})
    rc = mod.main(["check-heartbeat"], runner=runner, environ=hb_env(tmp_path), now_epoch=NOW)
    assert rc == 0
    assert read_outputs(tmp_path / "gh_out")["stale"] == "true"


def test_age_arithmetic_truncates_like_shell(mod: ModuleType, tmp_path: Path) -> None:
    """7385s = 2h 3m (integer division twice, seconds dropped)."""
    run_epoch = NOW - 7385
    payload = json.dumps({"updated_at": _iso(run_epoch)})
    runner = FakeRunner({("gh", "api"): (0, payload, "")})
    rc = mod.main(["check-heartbeat"], runner=runner, environ=hb_env(tmp_path), now_epoch=NOW)
    assert rc == 0
    assert read_outputs(tmp_path / "gh_out")["last_run_ago"] == "2h 3m ago"


@pytest.mark.parametrize("bad", ["0", "-2", "1.5", "", "2h"])
def test_invalid_windows_rejected(mod: ModuleType, tmp_path: Path, bad: str) -> None:
    runner = FakeRunner()
    rc = mod.main(
        ["check-heartbeat"],
        runner=runner,
        environ=hb_env(tmp_path, ALERT_WINDOW_HOURS=bad),
        now_epoch=NOW,
    )
    assert rc == 1
    assert runner.calls == []


def test_large_window_accepted(mod: ModuleType, tmp_path: Path) -> None:
    run_epoch = NOW - 20 * 3600
    payload = json.dumps({"updated_at": _iso(run_epoch)})
    runner = FakeRunner({("gh", "api"): (0, payload, "")})
    rc = mod.main(
        ["check-heartbeat"],
        runner=runner,
        environ=hb_env(tmp_path, ALERT_WINDOW_HOURS="24"),
        now_epoch=NOW,
    )
    assert rc == 0
    out = read_outputs(tmp_path / "gh_out")
    assert out["stale"] == "false"
    assert out["status_msg"].startswith("Reconciler healthy")


def test_github_output_appends_not_truncates(mod: ModuleType, tmp_path: Path) -> None:
    """$GITHUB_OUTPUT is append-mode: earlier keys written by other steps survive."""
    env = hb_env(tmp_path)
    Path(env["GITHUB_OUTPUT"]).write_text("earlier=kept\n", encoding="utf-8")
    runner = FakeRunner({("gh", "api"): (0, "", "")})
    rc = mod.main(["check-heartbeat"], runner=runner, environ=env, now_epoch=NOW)
    assert rc == 0
    out = read_outputs(tmp_path / "gh_out")
    assert out["earlier"] == "kept"
    assert out["stale"] == "true"


# --------------------------------------------------------------------------
# heartbeat-alert dispositions
# --------------------------------------------------------------------------


def test_find_failure_is_soft_open_proceeds(mod: ModuleType, tmp_path: Path) -> None:
    """rebar list failing -> treated as 'no existing alert' (YAML `|| echo ''`)."""
    runner = FakeRunner(
        {
            ("rebar", "list"): (1, "", "store busy"),
            ("rebar", "create"): (0, "", ""),
            ("gh", "api"): prev_red(),
        }
    )
    env = alert_env(tmp_path, **THRESHOLD_ENV)
    rc = mod.main(["heartbeat-alert"], runner=runner, environ=env, now_epoch=NOW)
    assert rc == 0
    assert [c[1] for c in runner.rebar_writes()] == ["create"]


def test_find_garbage_json_is_soft(mod: ModuleType, tmp_path: Path) -> None:
    runner = FakeRunner(
        {
            ("rebar", "list"): (0, "certainly not json", ""),
            ("rebar", "create"): (0, "", ""),
            ("gh", "api"): prev_red(),
        }
    )
    env = alert_env(tmp_path, **THRESHOLD_ENV)
    rc = mod.main(["heartbeat-alert"], runner=runner, environ=env, now_epoch=NOW)
    assert rc == 0
    assert [c[1] for c in runner.rebar_writes()] == ["create"]


def test_find_picks_first_ticket(mod: ModuleType, tmp_path: Path) -> None:
    listing = json.dumps([{"ticket_id": "aaaa-1"}, {"ticket_id": "bbbb-2"}])
    runner = FakeRunner({("rebar", "list"): (0, listing, "")})
    rc = mod.main(["heartbeat-alert"], runner=runner, environ=alert_env(tmp_path), now_epoch=NOW)
    assert rc == 0
    writes = runner.rebar_writes()
    assert writes and writes[0][1] == "comment" and writes[0][2] == "aaaa-1"


def test_no_op_when_fresh_and_none_open(mod: ModuleType, tmp_path: Path) -> None:
    runner = FakeRunner({("rebar", "list"): (0, "[]", "")})
    env = alert_env(tmp_path, STALE="false")
    rc = mod.main(["heartbeat-alert"], runner=runner, environ=env, now_epoch=NOW)
    assert rc == 0
    assert runner.rebar_writes() == []


@pytest.mark.parametrize("verb", ["create", "comment", "transition"])
def test_each_write_failure_fails_loud(mod: ModuleType, tmp_path: Path, verb: str) -> None:
    if verb == "create":
        listing, stale = "[]", "true"
    elif verb == "comment":
        listing, stale = json.dumps([{"ticket_id": "x-1"}]), "true"
    else:
        listing, stale = json.dumps([{"ticket_id": "x-1"}]), "false"
    runner = FakeRunner(
        {
            ("rebar", "list"): (0, listing, ""),
            ("rebar", verb): (1, "", "kaboom"),
            ("gh", "api"): prev_red(),
        }
    )
    env = alert_env(tmp_path, STALE=stale, **THRESHOLD_ENV)
    rc = mod.main(["heartbeat-alert"], runner=runner, environ=env, now_epoch=NOW)
    assert rc != 0


def test_close_argv_exact_shape(mod: ModuleType, tmp_path: Path) -> None:
    listing = json.dumps([{"ticket_id": "x-1"}])
    runner = FakeRunner({("rebar", "list"): (0, listing, "")})
    env = alert_env(tmp_path, STALE="false", STATUS_MSG="Reconciler healthy — ok.")
    rc = mod.main(["heartbeat-alert"], runner=runner, environ=env, now_epoch=NOW)
    assert rc == 0
    argv = runner.rebar_writes()[0]
    assert argv[0:5] == ["rebar", "transition", "x-1", "open", "closed"]
    i = argv.index("--class")
    assert argv[i + 1] == "env_integration"
    reason = argv[argv.index("--reason") + 1]
    assert reason.startswith("Fixed: ")
    force = [a for a in argv if a.startswith("--force-close=")]
    assert len(force) == 1 and force[0].split("=", 1)[1].startswith("Fixed: ")


def test_dry_run_skips_even_the_find(mod: ModuleType, tmp_path: Path) -> None:
    """DRY_RUN=true -> zero rebar WRITES (find may or may not run; writes must not)."""
    runner = FakeRunner({("rebar", "list"): (0, json.dumps([{"ticket_id": "x"}]), "")})
    env = alert_env(tmp_path, DRY_RUN="true", STALE="false")
    rc = mod.main(["heartbeat-alert"], runner=runner, environ=env, now_epoch=NOW)
    assert rc == 0
    assert runner.rebar_writes() == []


def test_comment_timestamp_derived_from_now_epoch(mod: ModuleType, tmp_path: Path) -> None:
    listing = json.dumps([{"ticket_id": "x-1"}])
    runner = FakeRunner({("rebar", "list"): (0, listing, "")})
    rc = mod.main(["heartbeat-alert"], runner=runner, environ=alert_env(tmp_path), now_epoch=NOW)
    assert rc == 0
    body = runner.rebar_writes()[0][3]
    assert _iso(NOW) in body


# --------------------------------------------------------------------------
# check-binding-drift edges
# --------------------------------------------------------------------------


def test_fsck_empty_stdout_degrades(mod: ModuleType, tmp_path: Path) -> None:
    runner = FakeRunner({("rebar", "bridge-fsck"): (1, "", "")})
    env = {"GITHUB_OUTPUT": str(tmp_path / "gh_out")}
    (tmp_path / "gh_out").touch()
    rc = mod.main(["check-binding-drift"], runner=runner, environ=env, now_epoch=NOW)
    assert rc == 0
    out = read_outputs(tmp_path / "gh_out")
    assert out["drift_found"] == "false"
    assert out["drift_total"] == "0"
    assert out["drift_summary"] == "none"


def test_fsck_missing_binding_drift_key(mod: ModuleType, tmp_path: Path) -> None:
    runner = FakeRunner({("rebar", "bridge-fsck"): (0, json.dumps({"other": 1}), "")})
    env = {"GITHUB_OUTPUT": str(tmp_path / "gh_out")}
    (tmp_path / "gh_out").touch()
    rc = mod.main(["check-binding-drift"], runner=runner, environ=env, now_epoch=NOW)
    assert rc == 0
    assert read_outputs(tmp_path / "gh_out")["drift_found"] == "false"


def test_summary_cell_order_is_canonical(mod: ModuleType, tmp_path: Path) -> None:
    """Summary lists cells in the fixed canonical order, only non-zero cells."""
    fsck = json.dumps(
        {
            "binding_drift": {
                "retired_overlap": ["r"],
                "unbound_jira": ["u1", "u2"],
                "would_terminal": [],
                "dangling": ["d"],
                "local_gone": [],
            }
        }
    )
    runner = FakeRunner({("rebar", "bridge-fsck"): (1, fsck, "")})
    env = {"GITHUB_OUTPUT": str(tmp_path / "gh_out")}
    (tmp_path / "gh_out").touch()
    rc = mod.main(["check-binding-drift"], runner=runner, environ=env, now_epoch=NOW)
    assert rc == 0
    out = read_outputs(tmp_path / "gh_out")
    assert out["drift_summary"] == "dangling=1, unbound_jira=2, retired_overlap=1"
    assert out["drift_total"] == "4"


def test_unknown_extra_cells_ignored(mod: ModuleType, tmp_path: Path) -> None:
    """Only the five canonical cells count (YAML parity: fixed tuple)."""
    fsck = json.dumps({"binding_drift": {"mystery": ["m"], "dangling": ["d"]}})
    runner = FakeRunner({("rebar", "bridge-fsck"): (1, fsck, "")})
    env = {"GITHUB_OUTPUT": str(tmp_path / "gh_out")}
    (tmp_path / "gh_out").touch()
    rc = mod.main(["check-binding-drift"], runner=runner, environ=env, now_epoch=NOW)
    assert rc == 0
    out = read_outputs(tmp_path / "gh_out")
    assert out["drift_total"] == "1"
    assert out["drift_summary"] == "dangling=1"


# --------------------------------------------------------------------------
# binding-drift-alert
# --------------------------------------------------------------------------


def test_drift_alert_comment_format(mod: ModuleType, tmp_path: Path) -> None:
    listing = json.dumps([{"ticket_id": "dd-1"}])
    runner = FakeRunner({("rebar", "list"): (0, listing, "")})
    rc = mod.main(
        ["binding-drift-alert"], runner=runner, environ=drift_env(tmp_path), now_epoch=NOW
    )
    assert rc == 0
    writes = runner.rebar_writes()
    assert [c[1] for c in writes] == ["comment"]
    body = writes[0][3]
    assert body.startswith("BRIDGE_CANARY_ALERT: Binding drift still present as of ")
    assert "would_terminal=2, local_gone=1" in body


def test_drift_alert_close_on_recovery(mod: ModuleType, tmp_path: Path) -> None:
    listing = json.dumps([{"ticket_id": "dd-1"}])
    runner = FakeRunner({("rebar", "list"): (0, listing, "")})
    env = drift_env(tmp_path, DRIFT_FOUND="false")
    rc = mod.main(["binding-drift-alert"], runner=runner, environ=env, now_epoch=NOW)
    assert rc == 0
    argv = runner.rebar_writes()[0]
    assert argv[1] == "transition"
    assert "--class" in argv and argv[argv.index("--class") + 1] == "env_integration"
    reason = argv[argv.index("--reason") + 1]
    assert reason.startswith("Fixed: bridge-fsck reports zero binding drift")


def test_drift_alert_dry_run_zero_writes(mod: ModuleType, tmp_path: Path) -> None:
    runner = FakeRunner({("rebar", "list"): (0, json.dumps([{"ticket_id": "dd-1"}]), "")})
    env = drift_env(tmp_path, DRY_RUN="true")
    rc = mod.main(["binding-drift-alert"], runner=runner, environ=env, now_epoch=NOW)
    assert rc == 0
    assert runner.rebar_writes() == []


def test_drift_alert_noop_when_clean_and_none(mod: ModuleType, tmp_path: Path) -> None:
    runner = FakeRunner({("rebar", "list"): (0, "[]", "")})
    env = drift_env(tmp_path, DRIFT_FOUND="false")
    rc = mod.main(["binding-drift-alert"], runner=runner, environ=env, now_epoch=NOW)
    assert rc == 0
    assert runner.rebar_writes() == []
