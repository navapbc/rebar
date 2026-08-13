"""Oracle suite for scripts/dependency_audit.py (bug 63e8-9235-220f-4201).

The defect this pins: a committed `uv.lock` turns any new advisory against a PINNED
transitive dependency into a build stoppage for every change in flight — six changes
across five work streams went red at once on click 8.2.1 / PYSEC-2026-2132, none of them
at fault. The fix routes the verdict by LANE, and the whole of it is:

    gerrit + does-not-touch-the-dependency-map  ->  NOT blocking
    everything else with a CRITICAL/HIGH finding ->  blocking

A workflow that is only eyeballed is how this class of defect survives, so the decision
logic lives in a module and these tests are what actually prove it. The three claims the
ticket demands by test are:

* a dependency-map-touching change IS blocked by an advisory
  — ``test_gerrit_lane_blocks_when_dependency_map_touched``
* a change that does NOT touch it is NOT blocked
  — ``test_gerrit_lane_does_not_block_when_map_untouched``
* the release gate blocks on an outstanding advisory
  — ``test_release_lane_blocks``

Seams (keyword-only params of ``main``): ``runner(argv) -> (rc, stdout, stderr)`` for
every external command (pip-audit, curl, git, rebar), ``environ``, ``now_epoch``.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = REPO_ROOT / "scripts" / "dependency_audit.py"


@pytest.fixture(scope="module")
def mod() -> ModuleType:
    # import_module (not spec_from_file_location) so the module lands in sys.modules —
    # @dataclass resolves its own module there while processing annotations.
    # tests/scripts/conftest.py puts repo-root scripts/ on sys.path.
    assert _SCRIPT.is_file()
    return importlib.import_module("dependency_audit")


class FakeRunner:
    """Replies from a prefix->response table; records every argv."""

    def __init__(self, responses: dict[tuple[str, ...], tuple[int, str, str]] | None = None):
        self.responses = responses or {}
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> tuple[int, str, str]:
        self.calls.append(list(argv))
        best: tuple[int, str, str] | None = None
        best_len = -1
        for prefix, response in self.responses.items():
            if tuple(argv[: len(prefix)]) == prefix and len(prefix) > best_len:
                best, best_len = response, len(prefix)
        return best if best is not None else (0, "", "")


def audit_json(*vulns: tuple[str, str, str, list[str]]) -> str:
    """Build pip-audit JSON from ``(package, version, vuln_id, fix_versions)`` tuples."""
    deps: list[dict[str, object]] = []
    for package, version, vuln_id, fixes in vulns:
        deps.append(
            {
                "name": package,
                "version": version,
                "vulns": [{"id": vuln_id, "fix_versions": fixes, "description": "x"}],
            }
        )
    return json.dumps({"dependencies": deps, "fixes": []})


CLICK_ADVISORY = ("click", "8.2.1", "PYSEC-2026-2132", ["8.3.0"])


def gate_runner(payload: str, *, severity: str | None = None) -> FakeRunner:
    """A runner whose pip-audit returns ``payload`` and whose OSV lookup is controllable."""
    responses: dict[tuple[str, ...], tuple[int, str, str]] = {
        ("pip-audit",): (1 if payload and '"vulns": [{' in payload else 0, payload, ""),
    }
    osv = json.dumps({"database_specific": {"severity": severity}}) if severity is not None else ""
    responses[("curl",)] = (0, osv, "") if severity is not None else (7, "", "unreachable")
    return FakeRunner(responses)


# ── The dependency-map signal ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path",
    [
        "uv.lock",
        "pyproject.toml",
        "./pyproject.toml",
        "packages/worker/pyproject.toml",
        "requirements.txt",
        "requirements-dev.txt",
        "constraints.txt",
        "vendor/uv.lock",
    ],
)
def test_touches_dependency_map_positive(mod: ModuleType, path: str) -> None:
    assert mod.touches_dependency_map([path]) is True


@pytest.mark.parametrize(
    "path",
    [
        "src/rebar/tickets.py",
        "docs/README.md",
        ".github/workflows/test.yml",
        "tests/unit/test_x.py",
        "pyproject.toml.bak",
        "notes/requirements.md",
    ],
)
def test_touches_dependency_map_negative(mod: ModuleType, path: str) -> None:
    assert mod.touches_dependency_map([path]) is False


def test_touches_dependency_map_any_path_in_a_mixed_change(mod: ModuleType) -> None:
    paths = ["src/rebar/tickets.py", "docs/README.md", "uv.lock"]
    assert mod.touches_dependency_map(paths) is True


def test_touches_dependency_map_ignores_blank_lines(mod: ModuleType) -> None:
    assert mod.touches_dependency_map(["", "  ", "src/rebar/x.py"]) is False


# ── Severity bar ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("severity", "expected"),
    [
        ("CRITICAL", "fail"),
        ("critical", "fail"),
        ("HIGH", "fail"),
        ("MEDIUM", "warn"),
        ("MODERATE", "warn"),
        ("LOW", "track"),
        ("", "fail"),  # unrated -> HIGH -> fail (the documented fallback)
        ("bogus", "fail"),
    ],
)
def test_classify(mod: ModuleType, severity: str, expected: str) -> None:
    assert mod.classify(severity) == expected


def test_unrated_advisory_is_treated_as_high(mod: ModuleType) -> None:
    """An advisory with no severity must never be silently ignored."""
    runner = gate_runner(audit_json(CLICK_ADVISORY))  # curl fails -> no severity
    findings = mod.collect_findings(runner, audit_json(CLICK_ADVISORY))
    assert len(findings) == 1
    assert findings[0].severity == mod.UNRATED_FALLBACK == "HIGH"
    assert findings[0].severity_source == "unrated-fallback"
    assert findings[0].disposition == "fail"


def test_osv_enrichment_downgrades_a_medium(mod: ModuleType) -> None:
    runner = gate_runner(audit_json(CLICK_ADVISORY), severity="MEDIUM")
    findings = mod.collect_findings(runner, audit_json(CLICK_ADVISORY))
    assert findings[0].severity == "MEDIUM"
    assert findings[0].severity_source == "osv"
    assert findings[0].disposition == "warn"


def test_osv_enrichment_failure_is_fail_soft_toward_strict(mod: ModuleType) -> None:
    """An OSV outage must make the gate STRICTER, never weaker."""
    runner = FakeRunner({("curl",): (0, "not json at all", "")})
    assert mod.osv_severity(runner, "PYSEC-2026-2132") == ""
    assert mod.classify("") == "fail"


# ── Lane verdicts — the three claims the ticket demands by test ─────────────────────


def _high_finding(mod: ModuleType) -> object:
    return mod.Finding(
        id="PYSEC-2026-2132",
        package="click",
        version="8.2.1",
        fix_versions=("8.3.0",),
        severity="HIGH",
        severity_source="osv",
    )


def test_gerrit_lane_blocks_when_dependency_map_touched(mod: ModuleType) -> None:
    verdict = mod.decide([_high_finding(mod)], lane="gerrit", touches_map=True)
    assert verdict.blocking is True
    assert verdict.fail_ids == ("PYSEC-2026-2132",)


def test_gerrit_lane_does_not_block_when_map_untouched(mod: ModuleType) -> None:
    """The click 8.2.1 failure mode: an unrelated change must not go red."""
    verdict = mod.decide([_high_finding(mod)], lane="gerrit", touches_map=False)
    assert verdict.blocking is False
    # The finding is still REPORTED — the lane split is about ownership, not suppression.
    assert verdict.fail_ids == ("PYSEC-2026-2132",)
    assert "does not touch the dependency map" in verdict.reason


def test_release_lane_blocks(mod: ModuleType) -> None:
    verdict = mod.decide([_high_finding(mod)], lane="release", touches_map=False)
    assert verdict.blocking is True


def test_branch_lane_blocks(mod: ModuleType) -> None:
    verdict = mod.decide([_high_finding(mod)], lane="branch", touches_map=False)
    assert verdict.blocking is True


@pytest.mark.parametrize("lane", ["gerrit", "branch", "release"])
def test_no_findings_never_blocks(mod: ModuleType, lane: str) -> None:
    assert mod.decide([], lane=lane, touches_map=True).blocking is False


@pytest.mark.parametrize("lane", ["gerrit", "branch", "release"])
def test_medium_only_never_blocks_any_lane(mod: ModuleType, lane: str) -> None:
    medium = mod.Finding("GHSA-m", "pkg", "1.0", (), "MEDIUM", "osv")
    verdict = mod.decide([medium], lane=lane, touches_map=True)
    assert verdict.blocking is False
    assert verdict.warn_ids == ("GHSA-m",)


@pytest.mark.parametrize("lane", ["gerrit", "branch", "release"])
def test_low_only_is_tracked_not_blocking(mod: ModuleType, lane: str) -> None:
    low = mod.Finding("GHSA-l", "pkg", "1.0", (), "LOW", "osv")
    verdict = mod.decide([low], lane=lane, touches_map=True)
    assert verdict.blocking is False
    assert verdict.track_ids == ("GHSA-l",)


def test_unknown_lane_is_rejected(mod: ModuleType) -> None:
    with pytest.raises(ValueError):
        mod.decide([], lane="whatever", touches_map=False)


# ── End-to-end through main() ──────────────────────────────────────────────────────


def test_gate_gerrit_exit_zero_for_an_unrelated_change(
    mod: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = gate_runner(audit_json(CLICK_ADVISORY), severity="HIGH")
    out_file = tmp_path / "gh_out"
    rc = mod.main(
        ["gate", "--lane", "gerrit", "--changed-files", "src/rebar/tickets.py\ndocs/x.md"],
        runner=runner,
        environ={"GITHUB_OUTPUT": str(out_file)},
        now_epoch=1_700_000_000,
    )
    assert rc == 0
    written = out_file.read_text()
    assert "blocking=false" in written
    assert "advisory_ids=PYSEC-2026-2132" in written
    captured = capsys.readouterr()
    assert "PYSEC-2026-2132" in captured.out  # still reported, never hidden
    assert mod.RUNBOOK in captured.out


def test_gate_gerrit_exit_one_when_lock_is_touched(
    mod: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = gate_runner(audit_json(CLICK_ADVISORY), severity="HIGH")
    out_file = tmp_path / "gh_out"
    rc = mod.main(
        ["gate", "--lane", "gerrit", "--changed-files", "uv.lock\npyproject.toml"],
        runner=runner,
        environ={"GITHUB_OUTPUT": str(out_file)},
        now_epoch=1_700_000_000,
    )
    assert rc == 1
    assert "blocking=true" in out_file.read_text()
    assert mod.RUNBOOK_URL in capsys.readouterr().err


def test_gate_release_exit_one_on_an_outstanding_advisory(
    mod: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = gate_runner(audit_json(CLICK_ADVISORY), severity="CRITICAL")
    rc = mod.main(["gate", "--lane", "release"], runner=runner, environ={}, now_epoch=0)
    assert rc == 1
    assert "never ships on a known-vulnerable pin" in capsys.readouterr().err


def test_gate_release_exit_zero_when_clean(mod: ModuleType) -> None:
    runner = gate_runner(json.dumps({"dependencies": [], "fixes": []}))
    assert mod.main(["gate", "--lane", "release"], runner=runner, environ={}, now_epoch=0) == 0


def test_gate_gerrit_uses_git_diff_when_changed_files_absent(mod: ModuleType) -> None:
    runner = gate_runner(audit_json(CLICK_ADVISORY), severity="HIGH")
    runner.responses[("git", "diff")] = (0, "uv.lock\nsrc/rebar/x.py\n", "")
    rc = mod.main(["gate", "--lane", "gerrit"], runner=runner, environ={}, now_epoch=0)
    assert rc == 1
    assert ["git", "diff", "--name-only", "HEAD^..HEAD"] in runner.calls


def test_gate_gerrit_fails_closed_when_the_diff_is_unresolvable(mod: ModuleType) -> None:
    """A shallow checkout must never be the reason an advisory goes unblocked."""
    runner = gate_runner(audit_json(CLICK_ADVISORY), severity="HIGH")
    runner.responses[("git", "diff")] = (128, "", "fatal: bad revision 'HEAD^'")
    rc = mod.main(["gate", "--lane", "gerrit"], runner=runner, environ={}, now_epoch=0)
    assert rc == 1


def test_gate_reports_db_unreachable_as_infrastructure(
    mod: ModuleType, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRunner({("pip-audit",): (1, "", "connection timed out reaching osv.dev")})
    # RECORD the backoff instead of sleeping it. `run_pip_audit` resolves its sleeper
    # at call time, so this patch reaches it; while the default was bound at import
    # time it did not, and the test really slept 5s + 10s (ticket 5ea3-76e5-480a-4464).
    delays: list[float] = []
    monkeypatch.setattr(mod.time, "sleep", delays.append)
    rc = mod.main(["gate", "--lane", "release"], runner=runner, environ={}, now_epoch=0)
    assert rc == 1
    err = capsys.readouterr().err
    assert "INFRASTRUCTURE issue" in err
    assert "recheck" in err
    assert sum(1 for call in runner.calls if call[0] == "pip-audit") == 3
    # The LOGICAL schedule (`attempt * 5`, no sleep after the last attempt). Doubles
    # as the guard: an early-bound seam leaves `delays` empty and fails here rather
    # than letting the test go quietly slow. A schedule, not a wall-clock budget.
    assert delays == [5, 10], f"expected 5s then 10s logical delays, got {delays}"


def test_a_real_finding_is_not_retried(mod: ModuleType) -> None:
    """`recheck` can never clear a real finding — so a finding must not loop the retry."""
    runner = gate_runner(audit_json(CLICK_ADVISORY), severity="HIGH")
    mod.main(["gate", "--lane", "release"], runner=runner, environ={}, now_epoch=0)
    assert sum(1 for call in runner.calls if call[0] == "pip-audit") == 1


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("connection reset by peer", True),
        ("HTTP 503 from the advisory DB", True),
        ("could not resolve host", True),
        ("Found 1 known vulnerability in 1 package", False),
    ],
)
def test_is_db_unreachable(mod: ModuleType, text: str, expected: bool) -> None:
    assert mod.is_db_unreachable(text) is expected


# ── Escalation lifecycle + dedup ───────────────────────────────────────────────────


BLOCKING_ENV = {
    "BLOCKING": "true",
    "ADVISORY_IDS": "PYSEC-2026-2132",
    "ADVISORY_SUMMARY": "click 8.2.1",
    "RUN_URL": "https://example/run/1",
}


def _rebar_calls(runner: FakeRunner, verb: str) -> list[list[str]]:
    return [c for c in runner.calls if c[:2] == ["rebar", verb]]


def test_advisory_alert_files_a_ticket_when_none_exists(mod: ModuleType) -> None:
    runner = FakeRunner({("rebar", "list"): (0, "[]", "")})
    assert mod.main(["advisory-alert"], runner=runner, environ=dict(BLOCKING_ENV), now_epoch=0) == 0
    created = _rebar_calls(runner, "create")
    assert len(created) == 1
    assert "--tags" in created[0] and "dependency-advisory-alert" in created[0]
    assert "--detected-by" in created[0]


def test_advisory_alert_does_not_file_a_second_ticket(mod: ModuleType) -> None:
    """DEDUP: a daily lane against a weeks-old advisory files ONE ticket, not N."""
    existing = json.dumps([{"ticket_id": "abcd-0000-1111-2222"}])
    runner = FakeRunner(
        {
            ("rebar", "list"): (0, existing, ""),
            ("rebar", "show"): (0, json.dumps({"comments": []}), ""),
        }
    )
    assert mod.main(["advisory-alert"], runner=runner, environ=dict(BLOCKING_ENV), now_epoch=0) == 0
    assert _rebar_calls(runner, "create") == []
    assert len(_rebar_calls(runner, "comment")) == 1


def test_advisory_alert_caps_accumulation_at_one_comment_per_day(mod: ModuleType) -> None:
    now = 1_700_000_000
    recent = {
        "comments": [
            {"body": f"{mod.ADVISORY_MARKER} still outstanding", "timestamp": (now - 3600) * 10**9}
        ]
    }
    runner = FakeRunner(
        {
            ("rebar", "list"): (0, json.dumps([{"ticket_id": "abcd-0000-1111-2222"}]), ""),
            ("rebar", "show"): (0, json.dumps(recent), ""),
        }
    )
    assert (
        mod.main(["advisory-alert"], runner=runner, environ=dict(BLOCKING_ENV), now_epoch=now) == 0
    )
    assert _rebar_calls(runner, "comment") == []


def test_advisory_alert_comments_again_after_the_window(mod: ModuleType) -> None:
    now = 1_700_000_000
    old = {
        "comments": [{"body": f"{mod.ADVISORY_MARKER} old", "timestamp": (now - 30 * 3600) * 10**9}]
    }
    runner = FakeRunner(
        {
            ("rebar", "list"): (0, json.dumps([{"ticket_id": "abcd-0000-1111-2222"}]), ""),
            ("rebar", "show"): (0, json.dumps(old), ""),
        }
    )
    mod.main(["advisory-alert"], runner=runner, environ=dict(BLOCKING_ENV), now_epoch=now)
    assert len(_rebar_calls(runner, "comment")) == 1


def test_advisory_alert_closes_the_ticket_when_the_advisory_clears(mod: ModuleType) -> None:
    runner = FakeRunner(
        {("rebar", "list"): (0, json.dumps([{"ticket_id": "abcd-0000-1111-2222"}]), "")}
    )
    env = {"BLOCKING": "false", "ADVISORY_IDS": "", "RUN_URL": ""}
    assert mod.main(["advisory-alert"], runner=runner, environ=env, now_epoch=0) == 0
    transitions = _rebar_calls(runner, "transition")
    assert len(transitions) == 1
    assert transitions[0][2:5] == ["abcd-0000-1111-2222", "open", "closed"]


def test_advisory_alert_close_argv_survives_the_real_transition_parser(
    mod: ModuleType,
) -> None:
    """The auto-close argv must be accepted by the REAL `rebar transition` flag parser.

    Regression for ticket 24f7. The advisory auto-close builds a `rebar transition …`
    argv by hand and this suite's `FakeRunner` never executes it, so the flags were only
    ever checked positionally (`transitions[0][2:5]`). When 24f7 renamed the close-gate
    escape hatch, this builder kept emitting the retired spelling and nothing here
    noticed — the break would only have surfaced in production, on the workflow run that
    tries to auto-close a cleared advisory.

    Asserting the exact flag string would just move the same blind spot. Instead, feed
    the generated argv to the actual parser: any future rename or removal of a flag this
    builder emits fails HERE, whatever it is renamed to.
    """
    from rebar._commands.transition import _parse_flags

    runner = FakeRunner(
        {("rebar", "list"): (0, json.dumps([{"ticket_id": "abcd-0000-1111-2222"}]), "")}
    )
    env = {"BLOCKING": "false", "ADVISORY_IDS": "", "RUN_URL": ""}
    assert mod.main(["advisory-alert"], runner=runner, environ=env, now_epoch=0) == 0

    argv = _rebar_calls(runner, "transition")[0]
    # `_parse_flags` consumes the args AFTER <ticket_id> <current> <target>; argv is
    # ["rebar", "transition", <tid>, <current>, <target>, *flags].
    _reason, force_reason, close_class, _caused_by, _ref = _parse_flags(argv[5:])

    assert close_class == "env_integration"
    assert force_reason, (
        "the auto-close must reach the parser as a gate bypass carrying a reason — an "
        "unrecognised flag would be silently skipped and the close would then hit the "
        "completion gate it is meant to bypass"
    )


def test_advisory_alert_refuses_to_file_a_hollow_ticket(mod: ModuleType) -> None:
    runner = FakeRunner({("rebar", "list"): (0, "[]", "")})
    env = {"BLOCKING": "true", "ADVISORY_IDS": "  ", "RUN_URL": ""}
    assert mod.main(["advisory-alert"], runner=runner, environ=env, now_epoch=0) == 1
    assert _rebar_calls(runner, "create") == []


def test_advisory_alert_dry_run_writes_nothing(mod: ModuleType) -> None:
    runner = FakeRunner()
    env = dict(BLOCKING_ENV) | {"DRY_RUN": "true"}
    assert mod.main(["advisory-alert"], runner=runner, environ=env, now_epoch=0) == 0
    assert runner.calls == []


def test_advisory_alert_write_failure_is_loud(mod: ModuleType) -> None:
    runner = FakeRunner(
        {("rebar", "list"): (0, "[]", ""), ("rebar", "create"): (3, "", "store locked")}
    )
    assert mod.main(["advisory-alert"], runner=runner, environ=dict(BLOCKING_ENV), now_epoch=0) == 3
