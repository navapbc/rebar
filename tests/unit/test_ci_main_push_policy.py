"""Main-branch CI is SCHEDULED, not push-triggered (ticket 03ef-6fb5-158b-4abd).

Gerrit's ``Verified`` label is the real merge gate: it runs the shared reusables against the
exact patchset BEFORE a change can land. The GitHub mirror's `main` CI is a post-merge safety
net whose value is "is `main` healthy NOW", not "was every commit green".

Running that safety net on every push to `main` had a concrete cost: ``test.yml`` (and
``prompt-eval.yml``, and ``codeql.yml``) declare ``cancel-in-progress: true`` with a
``github.ref``-keyed concurrency group, and on `main` that ref is the CONSTANT
``refs/heads/main`` — so every push lands in one group and CANCELS its predecessor. GitHub
renders a cancelled run as a red X and offers no native way to suppress that, so a perfectly
healthy `main` grew a trail of red X's.

The policy these tests pin:

* **No workflow runs on a push to ``refs/heads/main``.** The four ``branches-ignore``
  workflows list ``main`` next to ``feature/**``; the two ``branches: [main]`` workflows drop
  their ``push`` key outright.
* **Push CI for other branches is preserved** — only `main` (and the already-excluded
  S3-replicated ``feature/**``) is dropped.
* **A 6-hourly schedule at a distinct off-the-hour minute** carries the branch-health signal
  for the cheap always-on lanes, and ``workflow_dispatch`` answers "is main healthy?" on
  demand. CodeQL joined that tick in ticket 34b5-ad7d-b983-47f9 — see
  :func:`test_codeql_joined_the_six_hourly_branch_health_tick` for why.
* **Deliberately-paced lanes keep their own cadence** — the live-LLM prompt-eval tier stays
  weekly and terraform-drift stays daily, because for those two the frequency really is a
  cost/coverage tradeoff (billable model calls; a cloud drift sweep).
* **The separate mechanisms are untouched** — the Gerrit ``Verified`` lane, the reconcilers,
  the mirror guard, and the weekly external-integration tier.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Any

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOWS = _ROOT / ".github" / "workflows"

# The workflows whose purpose is "is `main` healthy" and which are cheap enough to run on the
# shared 6-hourly branch-health tick. Each keeps push CI for NON-main branches.
_SIX_HOURLY = ("test.yml", "optionality.yml", "verify-identity.yml")

# Also on the 6-hourly tick, but SCHEDULE-ONLY: these have no `push` trigger for any branch, so
# the "push CI for non-main branches survives" case below does not apply to them.
_SIX_HOURLY_SCHEDULE_ONLY = ("codeql.yml",)

# Every lane on the shared branch-health tick, however it is triggered otherwise.
_ALL_SIX_HOURLY = (*_SIX_HOURLY, *_SIX_HOURLY_SCHEDULE_ONLY)

# Workflows that must stop running on a push to `main` but keep their OWN, deliberately slower
# cadence: value is the cron that must survive this change.
_OWN_CADENCE = {
    "prompt-eval.yml": "0 7 * * 1",  # weekly — the live tier is billable
    "terraform-drift.yml": "17 6 * * *",  # daily — already its own drift sweep
}

# Every workflow whose `main`-push trigger this ticket removes.
_NO_MAIN_PUSH = (*_ALL_SIX_HOURLY, *_OWN_CADENCE)

# Separate mechanisms. Touching any of these is out of scope by operator instruction.
_MUST_NOT_CHANGE = (
    "gerrit-verify.yaml",
    "reconcile-bridge.yml",
    "reconcile-bridge-canary.yml",
    "external-integration.yml",
    "mirror-guard.yml",
    "_artifact-probe.yml",
    "_build-and-test.yml",
    "_eval-discipline.yml",
    "_optionality.yml",
)


def _load(name: str) -> dict[str, Any]:
    data = yaml.safe_load((_WORKFLOWS / name).read_text())
    assert isinstance(data, dict), f"{name} did not parse to a mapping"
    return data


def _on(name: str) -> dict[str, Any]:
    """The workflow's ``on:`` block.

    PyYAML resolves the bare key ``on`` to the boolean ``True`` (YAML 1.1 truthy), so read
    both spellings rather than assuming either.
    """
    data = _load(name)
    block = data.get("on", data.get(True))
    assert isinstance(block, dict), f"{name} has no mapping-shaped `on:` block"
    return block


def _crons(name: str) -> list[str]:
    schedule = _on(name).get("schedule") or []
    return [entry["cron"] for entry in schedule]


def _push_matches_branch(on_block: dict[str, Any], branch: str) -> bool:
    """Would a push to ``refs/heads/<branch>`` trigger a workflow with this ``on:`` block?

    Models GitHub's documented branch filtering: no ``push`` key => never; ``push`` with
    neither filter => every branch; ``branches`` => allow-list; ``branches-ignore`` =>
    deny-list. Patterns are glob-style, and ``**`` spans ``/`` (``fnmatch`` already treats
    ``*`` as spanning ``/``, which is why ``feature/**`` and ``feature/*`` both match a nested
    ``feature/a/b`` here — the cases under test never turn on that distinction).
    """
    push = on_block.get("push", "__absent__")
    if push == "__absent__":
        return False
    push = push or {}
    allow = push.get("branches")
    deny = push.get("branches-ignore")
    if allow is not None:
        return any(fnmatch.fnmatch(branch, pattern) for pattern in allow)
    if deny is not None:
        return not any(fnmatch.fnmatch(branch, pattern) for pattern in deny)
    return True


# --- AC1: nothing runs on a push to `main` -----------------------------------------------


@pytest.mark.parametrize("name", _NO_MAIN_PUSH)
def test_no_workflow_runs_on_a_push_to_main(name: str) -> None:
    assert not _push_matches_branch(_on(name), "main"), (
        f"{name} still triggers on a push to refs/heads/main; `main` CI is scheduled now "
        "(ticket 03ef-6fb5-158b-4abd)"
    )


# --- AC2: push CI for other branches survives --------------------------------------------


@pytest.mark.parametrize("name", (*_SIX_HOURLY, "prompt-eval.yml"))
def test_push_ci_for_non_main_branches_is_preserved(name: str) -> None:
    on_block = _on(name)
    assert _push_matches_branch(on_block, "wip-some-topic"), (
        f"{name} must still run on a push to a non-main topic branch"
    )
    assert not _push_matches_branch(on_block, "feature/big-thing"), (
        f"{name} must still skip the S3-replicated feature/** branches"
    )


# --- AC3/AC4/AC5: the cadences -----------------------------------------------------------


@pytest.mark.parametrize("name", _ALL_SIX_HOURLY)
def test_branch_health_lanes_run_every_six_hours_off_the_hour(name: str) -> None:
    crons = _crons(name)
    assert crons, f"{name} needs a schedule — it is the only remaining `main` health signal"
    six_hourly = [cron for cron in crons if cron.split()[1] == "*/6"]
    assert six_hourly, f"{name} has no 6-hourly cron (found {crons!r})"
    for cron in six_hourly:
        minute = cron.split()[0]
        assert minute.isdigit() and int(minute) not in (0, 30), (
            f"{name} schedules on minute {minute!r}; use an off-the-hour minute so this "
            "repo's runs do not pile onto the hour boundary with every other project"
        )


def test_six_hourly_lanes_are_staggered_on_distinct_minutes() -> None:
    """The tick's lanes must not all fire at once.

    GitHub queues scheduled workflows globally and drops or delays runs under load, so four
    lanes sharing a minute is the same self-inflicted pile-up as scheduling on the hour.
    """
    minutes = {
        name: sorted(
            {cron.split()[0] for cron in _crons(name) if cron.split()[1] == "*/6"},
        )
        for name in _ALL_SIX_HOURLY
    }
    flat = [minute for entry in minutes.values() for minute in entry]
    assert len(flat) == len(set(flat)), (
        f"the 6-hourly lanes collide on a minute: {minutes!r}; stagger them"
    )


def test_codeql_joined_the_six_hourly_branch_health_tick() -> None:
    """CodeQL moved off its weekly cron onto the shared tick (ticket 34b5-ad7d-b983-47f9).

    03ef removed CodeQL's push-to-`main` trigger, which had been its primary scan of newly
    landed code, and left only the weekly cron behind — silently stretching worst-case
    exposure for a newly-introduced SAST finding from "next push" to seven days. The 6-hourly
    tick is the specified replacement for push-on-`main`, and the frequency is not a billing
    question here: navapbc/rebar is a PUBLIC repository, where both CodeQL and Actions minutes
    are free.
    """
    crons = _crons("codeql.yml")
    assert crons == ["35 */6 * * *"], (
        "codeql.yml must schedule exactly the 6-hourly branch-health tick; the weekly "
        f"'23 4 * * 1' pass is superseded by it, not kept alongside it (found {crons!r})"
    )


@pytest.mark.parametrize(("name", "cron"), sorted(_OWN_CADENCE.items()))
def test_deliberately_paced_lanes_keep_their_own_cadence(name: str, cron: str) -> None:
    crons = _crons(name)
    assert cron in crons, f"{name} lost its own cadence {cron!r} (found {crons!r})"
    assert not any(entry.split()[1] == "*/6" for entry in crons), (
        f"{name} was moved onto the 6-hourly tick — that is a frequency INCREASE for a "
        "lane whose slower pace is deliberate (live-LLM cost / security-scan cadence)"
    )


# --- AC6: on-demand health check ---------------------------------------------------------


@pytest.mark.parametrize("name", _NO_MAIN_PUSH)
def test_every_changed_workflow_can_be_dispatched_on_demand(name: str) -> None:
    assert "workflow_dispatch" in _on(name), (
        f"{name} must keep workflow_dispatch so anyone can ask 'is main healthy?' without "
        "waiting for the scheduled tick"
    )


# --- AC7/AC8: the remediation recipe -----------------------------------------------------


def test_main_health_report_is_a_caller_level_job_that_survives_failure() -> None:
    jobs = _load("test.yml")["jobs"]
    assert "main-health-report" in jobs, (
        "test.yml needs a report job: a scheduled run covers many commits, so the failure "
        "output must hand the responder the bisect recipe and its lower bound"
    )
    report = jobs["main-health-report"]
    assert "always()" in report["if"], "the report must run even when the gates fail"
    assert set(report["needs"]) >= {"build-and-test", "golden-path", "artifact-probe"}, (
        "the report must observe every gating job's result"
    )
    assert report.get("permissions", {}).get("actions") == "read", (
        "resolving the last known-green run reads the Actions API"
    )
    assert "uses" not in report, (
        "the gates run inside shared reusables that take no caller steps, so the report has "
        "to be a caller-level job — putting it in a reusable would edit an out-of-scope file"
    )


def test_main_health_report_wires_the_renderer_to_the_run_summary() -> None:
    text = (_WORKFLOWS / "test.yml").read_text()
    assert (_ROOT / "scripts" / "main_health_report.py").is_file(), (
        "the rendering lives in a repo script so the suite can drive it — workflow YAML is "
        "not reachable from a test, and an unexercised bisect recipe fails first at 2am"
    )
    assert "scripts/main_health_report.py" in text, "the report job must invoke the renderer"
    assert "GITHUB_STEP_SUMMARY" in text, "the rendered report must land in the run summary"
    assert "GH_TOKEN" in text, (
        "the run-history lookup needs a token on the step; without one it 403s and the report "
        "degrades to its placeholder on every run instead of visibly once"
    )
    renderer = (_ROOT / "scripts" / "main_health_report.py").read_text()
    assert "status=success" in renderer, (
        "the last known-green SHA is resolved from this workflow's own successful run history"
    )


# --- AC9: the live external tier does not fire more often --------------------------------


def test_scheduled_and_plain_dispatch_runs_never_fire_the_live_external_tier() -> None:
    external_if = _load("test.yml")["jobs"]["external"]["if"]
    assert "workflow_dispatch" in external_if, "the live tier stays off the automated path"
    assert "run_external" in external_if, (
        "adding a schedule makes dispatch the routine 'is main healthy?' button, so the "
        "billable live tier needs an explicit opt-in rather than riding every dispatch"
    )
    inputs = _on("test.yml")["workflow_dispatch"]["inputs"]
    assert inputs["run_external"]["default"] in (False, "false"), (
        "the live external tier must be opt-IN"
    )


def test_external_integration_stays_weekly() -> None:
    assert _crons("external-integration.yml") == ["0 6 * * 1"], (
        "external-integration is capped at once a day and is weekly today; do not raise it"
    )


# --- AC10: the separate mechanisms are untouched -----------------------------------------


def test_gerrit_verify_stays_a_dispatch_only_patchset_gate() -> None:
    on_block = _on("gerrit-verify.yaml")
    assert set(on_block) == {"workflow_dispatch"}, (
        "gerrit-verify.yaml is triggered by Gerrit per patchset; it must never gain a "
        "schedule or a push trigger"
    )
    group = _load("gerrit-verify.yaml")["concurrency"]["group"]
    assert "GERRIT_CHANGE_ID" in group, (
        "the Verified lane's concurrency deliberately keys on the change, not the ref — that "
        "is its stale-patchset guard, not the red-X mechanism this ticket fixes"
    )


def test_reconcilers_keep_their_own_schedules_and_never_cancel() -> None:
    for name, cron in (
        ("reconcile-bridge.yml", "*/20 * * * *"),
        ("reconcile-bridge-canary.yml", "0 * * * *"),
    ):
        assert _crons(name) == [cron], f"{name} is a separate mechanism; leave it alone"
        assert _load(name)["concurrency"]["cancel-in-progress"] is False, (
            f"{name} must not cancel — a reconciler run is not superseded by a later one"
        )


@pytest.mark.parametrize("name", _MUST_NOT_CHANGE)
def test_out_of_scope_workflows_carry_no_branch_health_schedule(name: str) -> None:
    on_block = _on(name)
    assert not any(entry.split()[1] == "*/6" for entry in _crons(name)), (
        f"{name} is out of scope for the branch-health tick"
    )
    assert not _push_matches_branch(on_block, "main"), f"{name} must not run on a push to main"
