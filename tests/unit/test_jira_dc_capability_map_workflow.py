"""Config-as-artifact gate for the Jira DC capability-map workflow (ticket 259b-b7da-a346-4785).

The mapping job is explicitly an AUTHORING tool, never a test-path component (see the
workflow's own header comment and ``scripts/jira_dc_capability_map.py``'s docstring): its
acceptance criteria require that it "cannot run on push, PR, or schedule, and this is
asserted, not merely configured." This test is that assertion — it parses the workflow's
``on:`` trigger block directly (never trusting a code comment) and fails if anything other
than a bare ``workflow_dispatch`` ever appears there.

It also pins the harness-boot shape to the SAME digest-pinned Dockerfile the
``jira-dc-harness`` job in ``external-integration.yml`` builds, so a future edit cannot
quietly repoint this job at a different, unpinned image and still call itself "pinned by
digest" per the ticket's caveat.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _ROOT / ".github" / "workflows" / "jira-dc-capability-map.yml"
_HARNESS_DOCKERFILE = _ROOT / "tests" / "external" / "live_jira_dc" / "Dockerfile"
_MAP_SCRIPT = _ROOT / "scripts" / "jira_dc_capability_map.py"
_MAP_DOC = _ROOT / "docs" / "jira-dc-capability-map.md"

_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")


def _dockerfile_digest() -> str:
    """The ONE place the harness image is pinned: the vendored Dockerfile's FROM line."""
    for line in _HARNESS_DOCKERFILE.read_text().splitlines():
        if line.startswith("FROM"):
            found = _DIGEST_RE.search(line)
            if found:
                return found.group(0)
    raise AssertionError(f"{_HARNESS_DOCKERFILE} no longer pins its base image by digest")


def _load() -> dict:
    assert _WORKFLOW.exists(), f"expected workflow missing: {_WORKFLOW}"
    return yaml.safe_load(_WORKFLOW.read_text())


def test_dispatch_only_no_push_pr_or_schedule() -> None:
    doc = _load()
    # PyYAML parses the bare `on:` key as the boolean True when unquoted, per YAML 1.1's
    # implicit boolean scalars — a real, previously-observed footgun for exactly this kind
    # of trigger-block assertion, so this reads BOTH keys defensively.
    triggers = doc.get("on", doc.get(True))
    assert isinstance(triggers, dict), f"'on:' block is not a mapping: {triggers!r}"
    assert set(triggers) == {"workflow_dispatch"}, (
        f"jira-dc-capability-map.yml must be workflow_dispatch-ONLY (found trigger keys "
        f"{sorted(triggers)}) — this job is an authoring tool, never a test-path component, "
        f"and must never fire on push, pull_request, or a schedule (ticket 259b-b7da-a346-4785)"
    )


def test_no_write_permissions_granted() -> None:
    doc = _load()
    perms = doc.get("permissions") or {}
    assert perms.get("contents") == "read", (
        f"jira-dc-capability-map.yml should request only read access to repo contents "
        f"(found permissions={perms!r}) — it authors an artifact for a human to commit, "
        f"it does not commit anything itself"
    )


def test_boots_the_same_digest_pinned_harness_dockerfile() -> None:
    """The job must build tests/external/live_jira_dc/'s vendored Dockerfile — the ONE
    place the base image is pinned by digest — rather than a second, independently-pinned
    (and driftable) image reference."""
    assert _HARNESS_DOCKERFILE.exists(), f"harness Dockerfile missing: {_HARNESS_DOCKERFILE}"
    dockerfile_text = _HARNESS_DOCKERFILE.read_text()
    assert "@sha256:" in dockerfile_text.splitlines()[0] or any(
        line.startswith("FROM") and "@sha256:" in line for line in dockerfile_text.splitlines()
    ), f"{_HARNESS_DOCKERFILE} no longer pins its base image by digest"

    doc = _load()
    jobs = doc.get("jobs") or {}
    assert jobs, "workflow defines no jobs"
    run_steps: list[str] = []
    for job in jobs.values():
        for step in job.get("steps") or []:
            if isinstance(step, dict) and "run" in step:
                run_steps.append(step["run"])
    assert any("docker compose" in run and "--build" in run for run in run_steps), (
        "expected a step that builds the harness via `docker compose up -d --build` "
        "(against tests/external/live_jira_dc/Dockerfile) so the digest pin there applies here too"
    )


def test_script_is_invoked() -> None:
    doc = _load()
    jobs = doc.get("jobs") or {}
    run_steps: list[str] = []
    for job in jobs.values():
        for step in job.get("steps") or []:
            if isinstance(step, dict) and "run" in step:
                run_steps.append(step["run"])
    assert any("scripts/jira_dc_capability_map.py" in run for run in run_steps), (
        "workflow must invoke scripts/jira_dc_capability_map.py — the mapping tool "
        "cannot silently rot out of this job"
    )


# ---------------------------------------------------------------------------
# Digest PROVENANCE: the artifact must name the image it actually mapped, and the
# committed map must not silently outlive the pin it was measured against.
#
# "The map may be stale" is this ticket's design premise, not a hypothetical: the
# capability map has already carried a claim that did not reproduce (the retracted
# 254-char label ceiling). A map whose provenance is a hand-copied constant, and whose
# committed answers name no image at all, cannot even be checked for staleness — a
# re-pin lands and every recorded answer silently describes an image nothing runs.
# ---------------------------------------------------------------------------


def _run_map_script(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Drive the script through its CLI in a subprocess.

    Deliberately NOT an in-process import: ``tests/unit/`` contains its own
    ``rebar_reconciler/`` test package, and pytest puts the test's own directory on
    ``sys.path`` ahead of the engine, so under pytest ``import rebar_reconciler`` resolves
    to that shadowing test package and the script's engine imports fail with a spurious
    ``ModuleNotFoundError``. Driving the real CLI sidesteps the shadowing entirely AND
    exercises the script as CI actually invokes it — which matters because ``make lint``
    covers only ``src`` and ``tests``, so nothing else in the local gate would catch a
    breakage in this file before a billable live run hit it.
    """
    return subprocess.run(  # noqa: S603
        [sys.executable, str(_MAP_SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=str(cwd or _ROOT),
    )


def test_reported_digest_matches_the_dockerfile_pin() -> None:
    """Happy path: the digest the run reports IS the digest the harness builds."""
    done = _run_map_script("--print-digest")
    assert done.returncode == 0, f"--print-digest failed: {done.stderr}"
    assert done.stdout.strip() == _dockerfile_digest(), (
        f"the capability-map run reports {done.stdout.strip()!r} but "
        f"{_HARNESS_DOCKERFILE} pins {_dockerfile_digest()!r} — run_metadata.json would "
        f"mislabel which image was mapped, making the artifact's provenance record false"
    )


def test_reported_digest_follows_a_repinned_dockerfile(tmp_path: Path) -> None:
    """The load-bearing case: derivation, not duplication.

    A hardcoded constant passes the happy-path cell above and STILL misreports after a
    re-pin. Only a digest genuinely read from the Dockerfile follows a changed pin, so this
    cell is what distinguishes the two — point the script at a Dockerfile pinning a
    different digest and require the reported value to track it.
    """
    other = "sha256:" + "ab" * 32
    repinned = tmp_path / "Dockerfile"
    repinned.write_text(f"# a re-pinned harness\nFROM addono/jira-software-standalone@{other}\n")
    done = _run_map_script("--print-digest", "--dockerfile", str(repinned))
    assert done.returncode == 0, f"--print-digest failed: {done.stderr}"
    assert done.stdout.strip() == other, (
        f"reported {done.stdout.strip()!r} for a Dockerfile pinning {other!r} — the digest is "
        f"a hardcoded copy rather than a value read from the one place the pin lives, so a "
        f"future re-pin will silently mislabel the mapping run's provenance"
    )


def test_reported_digest_is_unknown_when_the_dockerfile_stops_pinning(tmp_path: Path) -> None:
    """Edge: an unpinned base image must be reported as unknown, never guessed.

    Reporting it as unknown (recorded as null in the metadata) is the honest answer;
    substituting a remembered digest would assert provenance the Dockerfile no longer
    supports. It must also not crash — aborting a ~35-minute live mapping run over a
    metadata detail would throw away the evidence the run exists to collect.
    """
    unpinned = tmp_path / "Dockerfile"
    unpinned.write_text("FROM addono/jira-software-standalone:latest\n")
    done = _run_map_script("--print-digest", "--dockerfile", str(unpinned))
    assert done.returncode == 0, f"exited {done.returncode} instead of reporting unknown"
    assert "Traceback" not in done.stderr, f"crashed on an unpinned Dockerfile: {done.stderr}"
    assert done.stdout.strip() == "unpinned", (
        f"expected the literal 'unpinned' for a tag-based FROM line, got {done.stdout.strip()!r}"
    )


def test_committed_map_records_the_digest_it_was_measured_against() -> None:
    """The staleness gate: the committed answers must name their image, and it must be
    the one we still pin.

    docs/jira-dc-capability-map.md warns that "a stale map after a re-pin describes an
    image nothing runs anymore" — but nothing enforced it, and the doc recorded no digest
    at all, so the warning was unfalsifiable. This cell fails the moment the Dockerfile is
    re-pinned without regenerating the map, which is exactly when the recorded answers stop
    describing reality.
    """
    assert _MAP_DOC.exists(), f"committed capability map missing: {_MAP_DOC}"
    digest = _dockerfile_digest()
    assert digest in _MAP_DOC.read_text(), (
        f"{_MAP_DOC} does not record the currently-pinned harness digest ({digest}). Either "
        f"the map was never told which image it measured, or the Dockerfile has been re-pinned "
        f"since — in which case every answer in that doc now describes an image nothing runs. "
        f"Re-dispatch .github/workflows/jira-dc-capability-map.yml against the new pin and "
        f"update the doc from the resulting artifact."
    )


def test_map_script_is_executable_and_fails_cleanly_offline(tmp_path: Path) -> None:
    """E2E on the script's own CLI contract.

    ``make lint`` covers only ``src`` and ``tests``, so nothing else in the local gate
    would catch a syntax or import error in this script — it would surface as a failed
    step partway into a live, billable CI run. ``--help`` proves the module imports and
    its parser builds; the offline invocation proves an unreachable harness is a clean
    non-zero exit rather than a traceback.
    """
    helped = _run_map_script("--help")
    assert helped.returncode == 0, f"--help failed: {helped.stderr}"
    assert "--output-dir" in helped.stdout

    # cwd/--output-dir both inside tmp_path: the default output dir is relative, and a run
    # rooted at the repo would leave a stray directory behind (the suite fails such leaks).
    offline = _run_map_script(
        "--base-url",
        "http://127.0.0.1:1",
        "--output-dir",
        str(tmp_path / "out"),
        cwd=tmp_path,
    )
    assert offline.returncode == 1, (
        f"expected a clean exit 1 against an unreachable harness, got "
        f"{offline.returncode}\nstdout={offline.stdout}\nstderr={offline.stderr}"
    )
    assert "Traceback" not in offline.stderr, (
        f"crashed instead of failing cleanly: {offline.stderr}"
    )


# ---------------------------------------------------------------------------
# Parity: the targeted Epic-Link probe (bug 1019) is a SECOND live-container
# authoring tool, so it inherits the same two safety rules.
# ---------------------------------------------------------------------------

_PROBE_WORKFLOW = _ROOT / ".github" / "workflows" / "jira-dc-epic-link-probe.yml"


def _load_probe() -> dict:
    assert _PROBE_WORKFLOW.exists(), f"expected workflow missing: {_PROBE_WORKFLOW}"
    return yaml.safe_load(_PROBE_WORKFLOW.read_text())


def test_probe_is_dispatch_only_no_push_pr_or_schedule() -> None:
    """Same rule, same reason: it boots a real Jira container.

    Added as its own cell rather than parametrizing the existing one so a future third tool
    cannot be added silently — a new live-container workflow with no guard is the failure this
    pair exists to prevent.
    """
    triggers = _load_probe().get("on", _load_probe().get(True))
    assert isinstance(triggers, dict), f"'on:' block is not a mapping: {triggers!r}"
    assert set(triggers) == {"workflow_dispatch"}, (
        f"jira-dc-epic-link-probe.yml must be workflow_dispatch-ONLY (found {sorted(triggers)}) "
        f"— it boots the pinned Jira DC container and must never fire on push, pull_request, or "
        f"a schedule (bug 1019-e1e9-5117-4795)"
    )


def test_probe_grants_no_write_permissions() -> None:
    perms = _load_probe().get("permissions") or {}
    assert perms.get("contents") == "read", (
        f"jira-dc-epic-link-probe.yml should request only read access (found {perms!r}) — it "
        f"answers two questions and uploads evidence; it commits nothing"
    )


def test_probe_does_not_invoke_the_capability_map() -> None:
    """The probe must NOT re-run the agentic capability map (operator constraint).

    The map's answers are already reviewed and committed in docs/jira-dc-capability-map.md;
    re-running it to settle two narrow questions would be slow, billable, and would risk
    churning landed data. Asserted on the workflow text so the constraint survives an edit.
    """
    text = _PROBE_WORKFLOW.read_text()
    assert "jira_dc_capability_map.py" not in text, (
        "the probe workflow invokes the capability-map script — it must run ONLY the targeted "
        "deterministic probe (scripts/jira_dc_epic_link_clear_probe.py)"
    )
    assert "jira_dc_epic_link_clear_probe.py" in text, (
        "the probe workflow does not run the probe script it exists for"
    )
    assert "ANTHROPIC_API_KEY" not in text, (
        "the probe requires no LLM key — its presence suggests an agentic path crept in"
    )
