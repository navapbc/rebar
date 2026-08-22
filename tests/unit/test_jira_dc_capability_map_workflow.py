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

import json
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
    return subprocess.run(
        [sys.executable, str(_MAP_SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=str(cwd or _ROOT),
        check=False,
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


# ---------------------------------------------------------------------------
# Post-run validation of the Epic-field answer (bug 4a6d-5bbc-44f4-4a56).
#
# The map's report is the authority a human uses to update _REQUIRED_FIELDS /
# _PROJECT_TEMPLATE in tests/external/live_jira_dc/conftest.py. "Epic Link absent"
# read before the agent's first Jira Software project create is indistinguishable
# from "this image dropped the Epic fields" — GreenHopper provisions those custom
# fields ON that create (run 30981084637, bug 941b-f049-5f29-4410). These cells pin
# that such an answer is REFUSED, and that a genuine degrade still passes through.
# ---------------------------------------------------------------------------

_SYSTEM_ONLY_FIELDS = [
    {"id": "summary", "name": "Summary"},
    {"id": "description", "name": "Description"},
    {"id": "issuetype", "name": "Issue Type"},
]

_PROVISIONED_FIELDS = [
    *_SYSTEM_ONLY_FIELDS,
    {"id": "customfield_10100", "name": "Sprint"},
    {"id": "customfield_10101", "name": "Story Points"},
]


def _map_module():
    """Import scripts/jira_dc_capability_map.py as a module (scripts/ is not a package)."""
    import importlib.util

    scripts_dir = str(_ROOT / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    spec = importlib.util.spec_from_file_location("_jira_dc_capability_map_uut", _MAP_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _field_read(body: object, status: int = 200) -> dict:
    return {
        "id": "req-0001",
        "method": "GET",
        "path": "/rest/api/2/field",
        "request_body": None,
        "status": status,
        "response_body": body,
    }


def test_absent_epic_ids_with_no_customfields_is_unverified() -> None:
    """The bug's own scenario: a system-only inventory cannot support an 'absent' claim."""
    mod = _map_module()
    problem = mod.epic_field_report_problem(
        {"image_digest": "sha256:abc", "jira_version": "9.4.0"},
        [_field_read(_SYSTEM_ONLY_FIELDS)],
    )
    assert problem is not None, (
        "a run that reports the Epic fields absent while its recorded inventory holds ZERO "
        "customfield_* entries must be refused — that inventory is the normal state of an "
        "instance with no Jira Software project yet (bug 941b-f049-5f29-4410), not evidence "
        "that the image dropped the fields"
    )
    assert "UNVERIFIED" in problem
    assert "epic_link_field_id" in problem and "epic_name_field_id" in problem
    # The verdict must carry the observation a human needs to act, not just a label.
    assert "Summary" in problem, f"the observed inventory is not quoted: {problem}"
    assert "30981084637" in problem, f"the measurement is not cited: {problem}"


def test_absent_epic_ids_with_other_customfields_is_accepted() -> None:
    """A genuine degrade must still pass: provisioning happened, the fields really are gone."""
    mod = _map_module()
    assert (
        mod.epic_field_report_problem(
            {"image_digest": "sha256:abc"}, [_field_read(_PROVISIONED_FIELDS)]
        )
        is None
    ), (
        "an inventory carrying OTHER customfield_* entries proves GreenHopper already "
        "provisioned, so absent Epic fields are a real image change — this run must not be "
        "refused"
    )


def test_reported_epic_ids_are_accepted_whatever_the_inventory() -> None:
    mod = _map_module()
    assert (
        mod.epic_field_report_problem(
            {"epic_link_field_id": "customfield_10102", "epic_name_field_id": "customfield_10103"},
            [_field_read(_SYSTEM_ONLY_FIELDS)],
        )
        is None
    ), "an answer that RESOLVED both Epic ids has nothing to validate"


def test_explicit_nulls_read_the_same_as_missing_keys() -> None:
    """`model_dump(exclude_none=True)` drops a null id, so both shapes must be equivalent."""
    mod = _map_module()
    explicit = mod.epic_field_report_problem(
        {"epic_link_field_id": None, "epic_name_field_id": None},
        [_field_read(_SYSTEM_ONLY_FIELDS)],
    )
    omitted = mod.epic_field_report_problem({}, [_field_read(_SYSTEM_ONLY_FIELDS)])
    assert explicit is not None and omitted is not None
    assert explicit == omitted


def test_one_absent_id_is_enough_to_refuse() -> None:
    mod = _map_module()
    problem = mod.epic_field_report_problem(
        {"epic_link_field_id": "customfield_10102"}, [_field_read(_SYSTEM_ONLY_FIELDS)]
    )
    assert problem is not None and "epic_name_field_id" in problem
    assert "epic_link_field_id" not in problem, (
        "only the id actually reported absent should be named in the verdict"
    )


def test_no_field_read_at_all_is_unverified() -> None:
    """Nothing observed is not the same as 'observed and healthy' — it is still unvouchable."""
    mod = _map_module()
    problem = mod.epic_field_report_problem(
        {}, [{"id": "req-0000", "method": "GET", "path": "/rest/api/2/serverInfo", "status": 200}]
    )
    assert problem is not None and "no usable GET /rest/api/2/field" in problem


def test_unusable_field_read_is_not_mistaken_for_an_empty_inventory() -> None:
    """A 401/503 must not read as 'this instance has no custom fields'."""
    mod = _map_module()
    problem = mod.epic_field_report_problem(
        {}, [_field_read({"errorMessages": ["denied"]}, status=401)]
    )
    assert problem is not None and "no usable GET /rest/api/2/field" in problem


def test_latest_field_read_wins() -> None:
    """The agent reads the inventory repeatedly; the post-create read is the authoritative one."""
    mod = _map_module()
    assert (
        mod.epic_field_report_problem(
            {}, [_field_read(_SYSTEM_ONLY_FIELDS), _field_read(_PROVISIONED_FIELDS)]
        )
        is None
    ), "the LAST usable inventory read must decide, not the first"


def test_script_adds_no_pre_create_field_wait() -> None:
    """The 941b ban holds: await_required_fields has no call site in this script.

    main() creates no Jira Software project before the agent run, so a wait here would poll
    for something only the blocked run can produce — the deadlock that bug 941b-f049-5f29-4410
    landed to remove (runs 30975323866, 30978613228).
    """
    text = _MAP_SCRIPT.read_text()
    assert "await_required_fields(" not in text, (
        "scripts/jira_dc_capability_map.py calls await_required_fields — that wait is "
        "post-project-create ONLY, and nothing in this script creates a project before the "
        "agent run, so it would burn its whole budget and abort every run "
        "(bug 941b-f049-5f29-4410)"
    )


def _drive_main(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, answer: dict, inventory: list):
    """Run ``main()`` with the LLM runtime, the PAT mint and the gate session faked out.

    Everything that needs a network, a model credential or a live Jira instance is replaced;
    what remains real is the code under test — the post-run validation and its effect on the
    exit code and the artifacts. Returns ``(exit_code, run_metadata)``.
    """
    import contextlib
    from types import SimpleNamespace

    mod = _map_module()
    cfg = SimpleNamespace(model="fake-model")

    monkeypatch.setattr(mod, "LLMConfig", SimpleNamespace(from_env=lambda **_: cfg))
    monkeypatch.setattr(mod, "RunRequest", lambda **kwargs: SimpleNamespace(**kwargs))
    monkeypatch.setattr(
        mod,
        "gate_source",
        SimpleNamespace(
            resolve_gate_handle=lambda **_: object(),
            apply_handle=lambda config, _handle: config,
            gate_read_root=lambda _handle: contextlib.nullcontext(),
        ),
    )
    monkeypatch.setattr(mod, "_mint_admin_pat", lambda *_a, **_k: "pat-token")
    monkeypatch.setattr(
        mod,
        "get_runner",
        lambda _cfg: SimpleNamespace(preflight=lambda: None, run=lambda _req: answer),
    )
    # The agent's field reads, as the evidence log would have recorded them.
    monkeypatch.setattr(mod, "_EVIDENCE", list(inventory))

    out = tmp_path / "out"
    code = mod.main(["--output-dir", str(out), "--base-url", "http://jira.invalid"])
    return code, json.loads((out / "run_metadata.json").read_text()), out


def test_main_fails_and_records_unverified_on_an_unvouchable_absence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The job must FAIL, and say so inside the artifact a human downloads."""
    code, metadata, out = _drive_main(
        monkeypatch, tmp_path, {"image_digest": "sha256:abc"}, [_field_read(_SYSTEM_ONLY_FIELDS)]
    )
    assert code != 0, (
        "a run whose Epic-field answer cannot be vouched for must fail the job — reporting it "
        "as a clean success is the whole defect (bug 4a6d-5bbc-44f4-4a56)"
    )
    assert metadata["epic_field_report"] == "unverified"
    assert "UNVERIFIED" in (metadata["epic_field_report_detail"] or "")
    # The artifacts still land: refusing the CLAIM must not destroy the EVIDENCE.
    assert (out / "capability_map.json").exists() and (out / "evidence.json").exists()


def test_main_succeeds_and_records_verified_on_a_vouchable_answer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Every other answer shape keeps today's behaviour: exit 0, same artifacts."""
    code, metadata, out = _drive_main(
        monkeypatch,
        tmp_path,
        {"image_digest": "sha256:abc"},
        [_field_read(_PROVISIONED_FIELDS)],
    )
    assert code == 0, "a genuine degrade (provisioned inventory, no Epic fields) must still pass"
    assert metadata["epic_field_report"] == "verified"
    assert metadata["epic_field_report_detail"] is None
    assert (out / "capability_map.json").exists()


def test_customfield_count_discriminates_the_two_causes() -> None:
    """The shared helper the verdict rests on, pinned directly.

    Zero vs non-zero is what separates "no Software project existed yet" from "this image
    dropped the fields"; an UNUSABLE read is neither, and must not collapse into zero.
    """
    mod = _map_module()
    readiness = mod.jira_dc_field_readiness
    assert readiness.customfield_count(200, _SYSTEM_ONLY_FIELDS) == 0
    assert readiness.customfield_count(200, _PROVISIONED_FIELDS) == 2
    assert readiness.customfield_count(401, {"errorMessages": ["denied"]}) is None, (
        "an unusable read must stay distinguishable from an inventory observed to be empty"
    )
    assert readiness.customfield_count(200, "not a list") is None


def test_capability_map_uses_the_shared_readiness_module() -> None:
    """Anti-drift: the discriminator must come from the module of record, not a local copy."""
    mod = _map_module()
    assert getattr(mod, "jira_dc_field_readiness", None) is not None, (
        "the capability map defines its own customfield_* discriminator instead of importing "
        "the shared scripts/jira_dc_field_readiness.py definition, so the two can drift"
    )
