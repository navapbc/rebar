"""Held-out provider, schema, and environment-parity contracts for the bridge runner."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
from pathlib import Path
from types import ModuleType

import jsonschema
import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
RUNNER = ROOT / "src" / "rebar" / "_bridge_runner.py"
GITHUB = ROOT / ".github" / "workflows" / "reconcile-bridge.yml"
JENKINS = ROOT / "Jenkinsfile"
GITLAB = ROOT / ".gitlab-ci.yml"
GITLAB_SCHEMA = ROOT / ".github" / "schemas" / "gitlab-ci.schema.json"
GITLAB_PROVENANCE = ROOT / ".github" / "schemas" / "gitlab-ci.schema.provenance.json"
PINNED_GITLAB_REVISION = "776c20ad5675eecea0c2010433a94d98d745e921"
PINNED_GITLAB_URL = (
    "https://gitlab.com/api/v4/projects/gitlab-org%2Fgitlab/repository/files/"
    "app%2Fassets%2Fjavascripts%2Feditor%2Fschema%2Fci.json/raw"
    f"?ref={PINNED_GITLAB_REVISION}"
)


def _load_runner() -> ModuleType:
    spec = importlib.util.spec_from_file_location("run_reconcile_bridge", RUNNER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _github_run_step() -> dict:
    workflow = yaml.safe_load(GITHUB.read_text(encoding="utf-8"))
    matches = [
        step
        for step in workflow["jobs"]["reconcile"]["steps"]
        if step.get("name") == "Run reconciler"
    ]
    assert len(matches) == 1
    return matches[0]


def test_runner_owns_shared_environment_and_timeout_contract() -> None:
    runner = _load_runner()

    assert runner.TIMEOUT_SECONDS == 3600
    assert set(runner.MODE_COMMANDS) == {
        "reconcile-check",
        "dry-run",
        "bootstrap-strict",
        "bootstrap-throttle",
        "live",
    }


def test_all_provider_wrappers_delegate_to_the_shared_runner() -> None:
    github_step = _github_run_step()
    assert github_step["run"].strip() == "rebar bridge run"
    assert github_step["id"] == "reconcile"
    assert github_step["env"]["BRIDGE_RUN_ID"] == "${{ github.run_id }}"

    jenkins = JENKINS.read_text(encoding="utf-8")
    assert jenkins.count("rebar bridge run") == 1
    assert "BUILD_TAG" in jenkins and "BRIDGE_RUN_ID" in jenkins
    assert "cron(" in jenkins

    gitlab = GITLAB.read_text(encoding="utf-8")
    assert gitlab.count("rebar bridge run") == 1
    assert "CI_PIPELINE_ID" in gitlab and "BRIDGE_RUN_ID" in gitlab
    assert "CI_PIPELINE_SOURCE" in gitlab and "schedule" in gitlab


def test_github_schedule_and_continuous_redispatch_remain_compatible() -> None:
    workflow = yaml.safe_load(GITHUB.read_text(encoding="utf-8"))
    assert workflow[True]["schedule"] == [{"cron": "23 * * * *"}]
    assert _github_run_step()["run"].strip() == "rebar bridge run"
    text = GITHUB.read_text(encoding="utf-8")
    assert "RECONCILE_CONTINUOUS" in text
    assert "gh workflow run reconcile-bridge.yml" in text
    assert "fetch-depth: 0" in text
    assert "+tickets:refs/remotes/origin/tickets" in text


def test_vendored_gitlab_schema_has_immutable_verified_provenance() -> None:
    schema_bytes = GITLAB_SCHEMA.read_bytes()
    schema = json.loads(schema_bytes)
    provenance = json.loads(GITLAB_PROVENANCE.read_text(encoding="utf-8"))

    assert provenance["source_url"] == PINNED_GITLAB_URL
    assert provenance["revision"] == PINNED_GITLAB_REVISION
    assert provenance["dialect"] == "http://json-schema.org/draft-07/schema#"
    assert provenance["sha256"] == hashlib.sha256(schema_bytes).hexdigest()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", provenance["retrieved_at"])
    assert schema["$schema"] == provenance["dialect"]
    jsonschema.Draft7Validator.check_schema(schema)


def test_gitlab_pipeline_validates_offline_against_vendored_draft7_schema() -> None:
    schema = json.loads(GITLAB_SCHEMA.read_text(encoding="utf-8"))
    pipeline = yaml.safe_load(GITLAB.read_text(encoding="utf-8"))

    jsonschema.Draft7Validator(schema).validate(pipeline)


def _shell_bodies() -> list[tuple[str, str]]:
    github = yaml.safe_load(GITHUB.read_text(encoding="utf-8"))
    bodies = [
        (f"github-{index}", step["run"])
        for index, step in enumerate(github["jobs"]["reconcile"]["steps"])
        if isinstance(step, dict) and isinstance(step.get("run"), str)
    ]

    jenkins = JENKINS.read_text(encoding="utf-8")
    bodies.extend(
        (f"jenkins-{index}", body)
        for index, body in enumerate(re.findall(r"sh\s+'''(.*?)'''", jenkins, re.DOTALL))
    )

    gitlab = yaml.safe_load(GITLAB.read_text(encoding="utf-8"))
    for job_name, job in gitlab.items():
        if not isinstance(job, dict) or "script" not in job:
            continue
        script = job["script"]
        body = script if isinstance(script, str) else "\n".join(script)
        bodies.append((f"gitlab-{job_name}", body))
    return bodies


def test_pinned_shellcheck_lints_all_provider_shell_bodies_without_skips(tmp_path: Path) -> None:
    binary = shutil.which("shellcheck")
    assert binary is not None, "shellcheck-py is a required test dependency, not an optional skip"
    version = subprocess.run(
        [binary, "--version"], capture_output=True, text=True, check=True
    ).stdout
    assert "version: 0.11.0" in version

    bodies = _shell_bodies()
    assert any(name.startswith("github-") for name, _ in bodies)
    assert any(name.startswith("jenkins-") for name, _ in bodies)
    assert any(name.startswith("gitlab-") for name, _ in bodies)
    for name, body in bodies:
        sanitized = re.sub(r"\$\{\{.*?\}\}", "provider_value", body)
        path = tmp_path / f"{name}.sh"
        path.write_text(sanitized, encoding="utf-8")
        completed = subprocess.run(
            [binary, "--shell=bash", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, f"{name}:\n{completed.stdout}{completed.stderr}"


@pytest.mark.parametrize(
    ("provider", "vendor_env"),
    [
        ("local", {"LOCAL_RUN": "1"}),
        ("github", {"GITHUB_RUN_ID": "vendor-id"}),
        ("jenkins", {"BUILD_TAG": "vendor-id"}),
        ("gitlab", {"CI_PIPELINE_ID": "vendor-id"}),
        ("bare", {}),
    ],
)
def test_five_environment_matrix_has_identical_results_without_gh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str, vendor_env: dict[str, str]
) -> None:
    # Load only the reusable setup harness; the expectations in this file remain held out.
    harness_path = ROOT / "tests" / "scripts" / "test_run_reconcile_bridge.py"
    spec = importlib.util.spec_from_file_location("bridge_harness", harness_path)
    assert spec is not None and spec.loader is not None
    harness = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(harness)

    # Simulate a hostile dev host: agent harnesses inject command-scope git
    # config through the environment (bug 3eb6-6e65).  The matrix must be
    # held out from this ambient state exactly as it is held out from gh.
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "safe.bareRepository")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "explicit")

    checkout, _tracker, origin = harness.bridge_workspace(tmp_path)
    env = harness.runner_env(tmp_path, checkout)
    for name in list(env):
        if name.startswith(("GITHUB_", "CI_", "BUILD_", "JENKINS_", "GITLAB_")):
            del env[name]
    env.update(vendor_env)
    env["BRIDGE_RUN_ID"] = f"matrix-{provider}"
    fake_bin = Path(env["REBAR_ARGV_FILE"]).parent / "bin"
    for executable in ("bash", "git"):
        resolved = shutil.which(executable)
        assert resolved is not None
        (fake_bin / executable).symlink_to(resolved)
    env["PATH"] = str(fake_bin)
    assert shutil.which("gh", path=env["PATH"]) is None

    completed = harness.run_bridge(checkout, env)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "Reconcile converged." in completed.stdout
    assert harness.git(origin, "show", "tickets:bridge-event.txt").stdout == ("event from bridge\n")
    assert harness.git(origin, "log", "-1", "--format=%s", "tickets").stdout.strip() == (
        f"chore: sync events from rebar reconciler [run matrix-{provider}]"
    )
