"""CI guard: the external suite's live-LLM lane is a real, correctly-wired PROVIDER MATRIX.

Story f124. Workflow YAML is otherwise unverifiable until it runs in CI — and this lane runs
weekly, on real money, so "we'll find out on the next schedule" is a month-long feedback loop on
a job whose failure mode is reporting GREEN. These tests read the committed workflow and the
committed overlay files and assert the properties the story's acceptance criteria name, plus one
behavioural test that runs the overlays through rebar's REAL config layering rather than
re-asserting the YAML back at itself.

What each group protects, and the specific way the matrix could be silently broken without it:

* **provider is an explicit matrix dimension** — otherwise the lane reverts to "whichever key
  happens to be set", which is the ambient default this story removes.
* **selection goes through REBAR_LLM_CONFIG_FILE, never the deprecated bare REBAR_LLM_MODEL** —
  the latter cannot express a per-class model, so it cannot repoint all three classes.
* **each overlay sets ONLY the two model-selection keys, `[llm] model` and
  [llm.model_classes]** — an overlay that also set, say, `max_steps` would make one arm differ
  from its siblings in more than the provider, and any difference found would be unattributable.
  BOTH selection keys are required, and an earlier version of this file asserted `model_classes`
  ALONE, which actively enforced a real leak: `cfg.model` is a second resolution path the class
  table cannot reach, so any op reading it called direct Anthropic on every arm (the f124
  incident). A test that pins the wrong surface is worse than no test, because it certifies
  the gap.
* **the overlay LAYERS rather than replaces** — the criterion `dict.update` cannot satisfy: an
  arm must override provider/model and leave the rest of the discovered config intact.
* **no arm holds a foreign provider's credential** — a Bedrock arm that also saw
  `ANTHROPIC_API_KEY` could fall back to direct Anthropic and the fallback would look like a
  pass.
* **the Bedrock arm names a mechanism that WORKS on a GitHub-hosted runner** — the S7 EC2
  instance-role/IMDS path is not reachable from `ubuntu-latest`, so OIDC role assumption must be
  present and must not have been quietly replaced by static keys.
* **the Bedrock arm sets BOTH region variables** — measured on ticket a574: IMDS supplies no
  region and rebar's own knob alone was insufficient.
* **an absent credential fails, loudly** — never a silent skip that renders as green.
* **the split preserves coverage** — the LLM lane and the services lane must partition the
  tier, not overlap it or drop part of it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import tomllib
import yaml

from rebar import config as root_cfg
from rebar.llm.config import LLMConfig

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _ROOT / ".github" / "workflows" / "external-integration.yml"
_EXTERNAL_TESTS = _ROOT / "tests" / "external"

_LLM_JOB = "external-llm"
_SERVICES_JOB = "external"

#: Providers the story requires live coverage for. OpenAI is additionally covered (the repo holds
#: an OPENAI_API_KEY) but is rebar's best-effort tier, so it is not required here: dropping it
#: should be a recorded decision, not a test failure.
_REQUIRED_PROVIDERS = {"anthropic", "bedrock"}

_CLASSES = ("trivial", "standard", "frontier")

#: Provider -> the env var carrying its API key. Bedrock is absent by design (ambient AWS chain).
_PROVIDER_KEY_ENV = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}

# The matrix dimension names the provider family; Pydantic AI's OpenAI provider has
# protocol-specific model qualifiers. Hosted OpenAI now deliberately selects Responses.
_MODEL_QUALIFIER_BY_PROVIDER = {"openai": "openai-responses"}


def _model_qualifier(provider: str) -> str:
    return _MODEL_QUALIFIER_BY_PROVIDER.get(provider, provider)


def _workflow() -> dict[str, Any]:
    return yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))


def _job(name: str) -> dict[str, Any]:
    jobs = _workflow()["jobs"]
    assert name in jobs, f"{_WORKFLOW.name} has no '{name}' job (jobs: {sorted(jobs)})"
    return jobs[name]


def _arms() -> list[dict[str, Any]]:
    include = _job(_LLM_JOB)["strategy"]["matrix"]["include"]
    assert isinstance(include, list) and include
    return include


def _steps(job_name: str) -> list[dict[str, Any]]:
    return list(_job(job_name)["steps"])


def _step_named(job_name: str, needle: str) -> dict[str, Any]:
    for step in _steps(job_name):
        if needle.lower() in str(step.get("name", "")).lower():
            return step
    raise AssertionError(f"no step whose name contains {needle!r} in job {job_name!r}")


def _suite_step() -> dict[str, Any]:
    return _step_named(_LLM_JOB, "Run the live-LLM suite")


# ── provider is an explicit matrix dimension ───────────────────────────────────────────
def test_the_llm_lane_runs_more_than_one_provider() -> None:
    providers = [arm["provider"] for arm in _arms()]
    assert len(providers) == len(set(providers)), f"duplicate matrix arms: {providers}"
    assert len(providers) > 1, (
        "the live-LLM lane has a single arm — provider must be a MATRIX dimension so a "
        f"provider-specific regression is detectable (arms: {providers})"
    )


def test_anthropic_and_bedrock_are_both_covered() -> None:
    providers = {arm["provider"] for arm in _arms()}
    missing = _REQUIRED_PROVIDERS - providers
    assert not missing, f"first-class providers with no live arm: {sorted(missing)}"


# ── selection goes through the layered config-file pointer ─────────────────────────────
def test_every_arm_names_an_existing_overlay_file() -> None:
    for arm in _arms():
        path = _ROOT / arm["config_file"]
        assert path.is_file(), f"arm {arm['provider']!r} points at a missing file: {path}"


def test_the_suite_step_selects_the_provider_via_the_config_file_pointer() -> None:
    env = _suite_step()["env"]
    pointer = env.get("REBAR_LLM_CONFIG_FILE")
    assert pointer, "the suite step sets no REBAR_LLM_CONFIG_FILE — no provider is selected"
    assert "matrix.config_file" in pointer, (
        "REBAR_LLM_CONFIG_FILE must be derived from the matrix arm's config_file, else every "
        f"arm points at the same overlay (got {pointer!r})"
    )


def test_the_deprecated_bare_model_variable_is_never_set() -> None:
    """`REBAR_LLM_MODEL` applies ONE model to all three classes, so it cannot express a
    per-class provider selection. It is deprecated (ADR 0057) and must not be the instrument.

    Checked against declared ENV KEYS (job- and step-level) across every job, not the raw file
    text — the variable is legitimately *named* in the prose explaining why it is not used.
    """
    for job_name, job in _workflow()["jobs"].items():
        assert "REBAR_LLM_MODEL" not in (job.get("env") or {}), f"job {job_name} sets it"
        for step in job.get("steps") or []:
            assert "REBAR_LLM_MODEL" not in (step.get("env") or {}), (
                f"step {step.get('name')!r} in job {job_name} sets the deprecated "
                "REBAR_LLM_MODEL instead of a REBAR_LLM_CONFIG_FILE overlay"
            )


def test_each_overlay_sets_only_the_model_selection_keys() -> None:
    """An overlay sets BOTH selection keys and nothing else.

    `model_classes` alone is NOT sufficient, and asserting only it is what let a real leak ship:
    `cfg.model` is a separate resolution path that falls back to the bare literal DEFAULT_MODEL and
    therefore infers provider `anthropic`, so every op reading it called direct Anthropic on all
    three arms while this file's class assertion passed (the f124 incident). Both keys are pinned
    here; unrelated keys are still forbidden, which is the original and still-valid intent."""
    for arm in _arms():
        data = tomllib.loads((_ROOT / arm["config_file"]).read_text(encoding="utf-8"))
        assert set(data) == {"llm"}, (
            f"{arm['config_file']} sets top-level keys other than [llm]: {sorted(data)}"
        )
        assert set(data["llm"]) == {"model", "model_classes"}, (
            f"{arm['config_file']} must set EXACTLY [llm] model + model_classes, got "
            f"{sorted(data['llm'])} — an arm must differ from siblings ONLY in provider, and it "
            f"must repoint BOTH the class table and the ambient cfg.model"
        )
        qualifier = _model_qualifier(arm["provider"])
        assert data["llm"]["model"].startswith(f"{qualifier}:"), (
            f"{arm['config_file']} sets [llm] model = {data['llm']['model']!r}, which is not "
            f"qualified with this arm's model protocol {qualifier!r}"
        )
        assert set(data["llm"]["model_classes"]) == set(_CLASSES), (
            f"{arm['config_file']} must set all three model classes, got "
            f"{sorted(data['llm']['model_classes'])}"
        )


# ── the overlay LAYERS: it repoints provider/model and leaves everything else intact ───
@pytest.fixture
def _clean_config_env(monkeypatch: pytest.MonkeyPatch):
    for name in (
        "REBAR_CONFIG",
        "REBAR_LLM_CONFIG_FILE",
        "REBAR_ROOT",
        "REBAR_LLM_MODEL",
        "REBAR_LLM_MAX_STEPS",
        "XDG_CONFIG_HOME",
    ):
        monkeypatch.delenv(name, raising=False)
    root_cfg.set_cli_overrides(None)
    yield
    root_cfg.reset_config_cache()


@pytest.mark.parametrize("arm", _arms(), ids=lambda a: str(a["provider"]))
def test_an_overlay_repoints_every_class_and_preserves_the_discovered_config(
    arm: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _clean_config_env: None,
) -> None:
    """THE acceptance criterion for the pointer, exercised through the REAL config layering.

    A discovered project config carries an unrelated `[llm]` key AND its own model classes. The
    arm's overlay must (a) repoint all three classes at the arm's provider and (b) leave the
    unrelated key at its discovered value. A REPLACE implementation reverts (b) to the built-in
    default and fails here; a shallow merge over `model_classes` would drop sibling class slots.
    """
    from rebar.llm import model_classes

    project = tmp_path / "proj"
    project.mkdir()
    (project / ".git").mkdir()
    (project / "rebar.toml").write_text(
        "[llm]\n"
        "max_steps = 7\n"
        "timeout = 123\n"
        "[llm.model_classes]\n"
        "frontier = { model = 'discovered:frontier-model' }\n"
        "standard = { model = 'discovered:standard-model' }\n"
        "trivial  = { model = 'discovered:trivial-model' }\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("REBAR_LLM_CONFIG_FILE", str(_ROOT / arm["config_file"]))
    root_cfg.reset_config_cache()

    slots = model_classes.load_class_slots(str(project))
    for name in _CLASSES:
        resolved = model_classes.resolve_class(name, slots)
        qualifier = _model_qualifier(arm["provider"])
        assert resolved.startswith(f"{qualifier}:"), (
            f"overlay {arm['config_file']} left class {name!r} on {resolved!r} instead of "
            f"model protocol {qualifier!r}"
        )

    # (b) the rest of the discovered config SURVIVES the overlay.
    root_cfg.reset_config_cache()
    cfg = LLMConfig.from_env(repo_root=project)
    assert cfg.max_iterations == 7, (
        "the overlay clobbered the discovered llm.max_steps — it must LAYER per key, not "
        "replace the discovered config"
    )
    assert cfg.timeout_s == 123, "the overlay clobbered the discovered llm.timeout"


# ── no arm may hold a foreign provider's credential ────────────────────────────────────
@pytest.mark.parametrize("key_env", sorted(_PROVIDER_KEY_ENV.values()))
def test_every_api_key_expression_is_guarded_on_the_arms_provider(key_env: str) -> None:
    """Each key must be conditioned on `matrix.provider == '<its provider>'`.

    This is the static half of "the Bedrock arm does not silently fall back to
    ANTHROPIC_API_KEY": an unguarded `secrets.ANTHROPIC_API_KEY` would hand the key to EVERY
    arm, and any code path that reads a key rather than the resolved model string could then
    answer from the wrong provider while the arm reported a pass.
    """
    owner = next(p for p, name in _PROVIDER_KEY_ENV.items() if name == key_env)
    guard = f"matrix.provider == '{owner}'"
    seen = 0
    for step in _steps(_LLM_JOB):
        expr = (step.get("env") or {}).get(key_env)
        if expr is None:
            continue
        seen += 1
        assert guard in str(expr), (
            f"{key_env} in step {step.get('name')!r} is not guarded by `{guard}` — every arm "
            f"would receive it (got {expr!r})"
        )
    assert seen, f"{key_env} is never provided to the {_LLM_JOB} job at all"


def test_the_bedrock_arm_receives_no_llm_api_key() -> None:
    """The same property read from the arm's side: for provider 'bedrock', every key
    expression's guard names a DIFFERENT provider, so the realised value is the empty string."""
    for key_env, owner in ((v, k) for k, v in _PROVIDER_KEY_ENV.items()):
        for step in _steps(_LLM_JOB):
            expr = (step.get("env") or {}).get(key_env)
            if expr is None:
                continue
            assert f"matrix.provider == '{owner}'" in str(expr) and owner != "bedrock", (
                f"{key_env} could reach the bedrock arm via {expr!r}"
            )


# ── the Bedrock arm's credential mechanism must work on a GitHub-hosted runner ─────────
def test_the_bedrock_arm_assumes_a_role_over_oidc() -> None:
    """S7's EC2 instance role is reached over IMDS from inside a container on that host; there
    is no instance role and no IMDS path to it from `ubuntu-latest`. So the arm must federate:
    `id-token: write` + `configure-aws-credentials` with a `role-to-assume`."""
    job = _job(_LLM_JOB)
    assert job.get("permissions", {}).get("id-token") == "write", (
        "the LLM job lacks `id-token: write`, so AssumeRoleWithWebIdentity cannot be performed"
    )
    creds = [
        s
        for s in _steps(_LLM_JOB)
        if "aws-actions/configure-aws-credentials" in str(s.get("uses", ""))
    ]
    assert len(creds) == 1, "expected exactly one configure-aws-credentials step"
    step = creds[0]
    assert "matrix.provider == 'bedrock'" in str(step.get("if", "")), (
        "the AWS credential step must be gated to the bedrock arm"
    )
    role = str(step["with"]["role-to-assume"])
    assert "AWS_BEDROCK_CI_ROLE_ARN" in role, (
        f"the assumed role must come from the AWS_BEDROCK_CI_ROLE_ARN repository variable "
        f"(got {role!r})"
    )
    assert step["with"].get("aws-region"), "configure-aws-credentials needs an aws-region"


def test_no_static_aws_keys_are_configured_anywhere() -> None:
    """The rejected alternative: a long-lived AWS access key in a repository secret. If it ever
    appears, the OIDC assertions above would still pass while the durable credential existed."""
    text = _WORKFLOW.read_text(encoding="utf-8")
    for forbidden in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        assert forbidden not in text, (
            f"{forbidden} appears in {_WORKFLOW.name} — Bedrock CI auth must be OIDC role "
            "assumption, not a long-lived key"
        )


# ── the Bedrock arm must resolve a region EXPLICITLY, from both variables ──────────────
def test_the_bedrock_arm_sets_both_region_variables() -> None:
    """Ticket a574, by measurement: IMDS supplies NO region (so a working credential chain does
    not imply a resolvable one), and rebar's own REBAR_LLM_BEDROCK_REGION alone was ALSO
    insufficient — AWS_DEFAULT_REGION was required too. Both, from the same source."""
    env = _suite_step()["env"]
    for name in ("AWS_DEFAULT_REGION", "REBAR_LLM_BEDROCK_REGION"):
        expr = env.get(name)
        assert expr, f"the suite step does not set {name}"
        assert "AWS_BEDROCK_CI_REGION" in str(expr), (
            f"{name} must come from the AWS_BEDROCK_CI_REGION repository variable, so both "
            f"region settings cannot drift apart (got {expr!r})"
        )
    assert env["AWS_DEFAULT_REGION"] == env["REBAR_LLM_BEDROCK_REGION"], (
        "the two region settings resolve differently — they must be the same value"
    )


def test_every_arm_resolves_a_region_not_only_the_bedrock_arm() -> None:
    """Bug 79d6. A live-LLM module may pin a `bedrock:` model whichever arm runs it —
    tests/external/test_completion_banking_behavior_0707.py pins
    `bedrock:us.anthropic.claude-sonnet-4-6`. While both region vars were guarded on
    `matrix.provider == 'bedrock'` they evaluated to the empty string on the anthropic and
    openai arms, and every such cell failed identically on region resolution (run
    31587452003). A region is a repository VARIABLE, not a credential, so the guard bought
    no credential isolation — and a fallback keeps the arms working when the variable is
    unset."""
    env = _suite_step()["env"]
    for name in ("AWS_DEFAULT_REGION", "REBAR_LLM_BEDROCK_REGION"):
        expr = str(env[name])
        assert "matrix.provider" not in expr, (
            f"{name} is guarded on the arm ({expr!r}) — bedrock-pinned live-LLM modules then "
            "reach build_bedrock_provider with no region on every non-bedrock arm"
        )
        assert "us-east-1" in expr, (
            f"{name} has no literal fallback ({expr!r}) — with AWS_BEDROCK_CI_REGION unset the "
            "arms resolve no region at all"
        )


# ── an absent credential fails LOUDLY; it never skips to green ─────────────────────────
def test_the_credential_preflight_fails_loudly_for_every_arm() -> None:
    step = _step_named(_LLM_JOB, "Require this arm's credential")
    script = str(step["run"])
    assert "exit 1" in script, (
        "the credential preflight does not exit non-zero — an arm with no credential would "
        "run, skip every live test, and report green"
    )
    assert "::error" in script, "the preflight must emit a GitHub ::error:: annotation"
    for provider in (arm["provider"] for arm in _arms()):
        assert provider in script, (
            f"the preflight has no credential rule for the {provider!r} arm, so that arm can "
            "run uncredentialed"
        )
    # The bedrock arm's prerequisites are variables, not a secret — check both are required.
    for var in ("AWS_BEDROCK_CI_ROLE_ARN", "AWS_BEDROCK_CI_REGION"):
        assert var in script, f"the preflight does not require {var} for the bedrock arm"


def test_the_arm_declares_its_provider_to_the_tests() -> None:
    """`REBAR_EXPECTED_LLM_PROVIDER` is what lets a test inside the arm prove the arm ran the
    provider it claims (tests/external/test_provider_matrix_live.py). Without it a mis-pathed
    pointer would silently run every arm on the default provider, all green."""
    expr = _suite_step()["env"].get("REBAR_EXPECTED_LLM_PROVIDER")
    assert expr and "matrix.provider" in str(expr)


# ── the split partitions the tier: no overlap, nothing dropped ─────────────────────────
def test_the_two_lanes_partition_the_external_tier() -> None:
    llm_run = str(_suite_step()["run"])
    assert '-m "external and llm_live"' in llm_run, (
        f"the LLM lane must select on the llm_live marker (got: {llm_run!r})"
    )
    services = _step_named(_SERVICES_JOB, "Run the external-integration suite")
    assert '-m "external and not llm_live"' in str(services["run"]), (
        "the services lane must select the COMPLEMENT of the LLM lane, or the live-LLM tests "
        "run twice (double billing) and mask the Jira all-skip canary"
    )


def test_the_usage_log_is_recorded_per_provider() -> None:
    """The cadence decision in docs/ci-provider-matrix.md is measured from these logs, so a
    shared path/artifact name across arms would make per-provider cost unknowable (and, for the
    artifact, is a hard upload conflict)."""
    log = str(_suite_step()["env"]["REBAR_USAGE_LOG"])
    assert "matrix.provider" in log, f"the usage log path is not per-arm (got {log!r})"
    upload = next(s for s in _steps(_LLM_JOB) if "upload-artifact" in str(s.get("uses", "")))
    assert "matrix.provider" in str(upload["with"]["name"])


# ── the marker automation cannot silently lose a module ────────────────────────────────
def test_every_module_using_the_shared_live_gate_declares_the_marker_sentinel() -> None:
    """A module that imports the shared live-LLM gate but forgets `_live_llm_ready` is NOT
    auto-marked `llm_live`, so the provider matrix would stop running it — silently, with no
    failing test anywhere. This is that failing test."""
    offenders = []
    for path in sorted(_EXTERNAL_TESTS.glob("test_*.py")):
        text = path.read_text(encoding="utf-8")
        if "_live_llm" not in text:
            continue
        if "_live_llm_ready" not in text:
            offenders.append(path.name)
    assert not offenders, (
        f"these external modules use the shared live-LLM gate but declare no "
        f"`_live_llm_ready` sentinel, so they are not auto-marked llm_live and the provider "
        f"matrix skips them entirely: {offenders}"
    )


# ── a module that PINS a provider is ready only on the arm that runs it ────────────────
def _live_llm_module():
    """Import tests/external/_live_llm.py by path (the external tier is not a package)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_live_llm_under_test", _EXTERNAL_TESTS / "_live_llm.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_a_pinned_provider_module_is_not_ready_on_a_mismatched_arm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug 4f74. `test_completion_banking_behavior_0707.py` pins
    `bedrock:us.anthropic.claude-sonnet-4-6`, so it calls Bedrock on EVERY arm — but the OIDC
    credential step is gated to the bedrock arm. While its sentinel asked the plain probe
    ('is the ARM's credential present'), the anthropic arm reported READY and then ran cells
    against a provider it holds no credential for. Readiness must be asked about the pinned
    provider, and the skip must NAME the mismatch rather than vanish silently."""
    mod = _live_llm_module()
    monkeypatch.setattr(mod, "agents_extra_installed", lambda: True)
    monkeypatch.setattr(mod, "configured_provider", lambda repo_root=None: "anthropic")
    monkeypatch.setattr(mod, "credential_present", lambda provider: True)

    assert mod.live_llm_ready("bedrock") is False, (
        "a bedrock-pinned module reports ready on the anthropic arm — the arm holds no AWS "
        "credential, so every pinned cell would fail on a provider it never claimed to cover"
    )
    reason = mod._skip_reason("bedrock")
    assert "bedrock" in reason and "anthropic" in reason, (
        f"the skip reason must name BOTH the pinned provider and the arm's resolved one so the "
        f"skip is visible and diagnosable (got {reason!r})"
    )
    # The unpinned probe is unchanged: modules that follow the arm keep their behaviour.
    assert mod.live_llm_ready() is True


def test_a_pinned_provider_module_is_ready_on_its_own_arm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The complement: the gate must not be a blanket disable. On the matching arm, with the
    provider's credential present, the pinned module still runs — otherwise the eval would
    silently stop covering anything."""
    mod = _live_llm_module()
    monkeypatch.setattr(mod, "agents_extra_installed", lambda: True)
    monkeypatch.setattr(mod, "configured_provider", lambda repo_root=None: "bedrock")
    monkeypatch.setattr(mod, "credential_present", lambda provider: provider == "bedrock")

    assert mod.live_llm_ready("bedrock") is True, (
        "the bedrock-pinned module does not run on the bedrock arm — the gate has disabled the "
        "eval everywhere instead of routing it to the arm that can run it"
    )


def test_a_module_pinning_a_provider_asks_the_probe_about_that_provider() -> None:
    """The invariant that failed in bug 4f74, pinned structurally so the NEXT module to pin a
    model cannot reintroduce it.

    A module whose source hard-codes a `<provider>:` model string does not follow the arm's
    `standard` model class, so a bare `live_llm_ready()` — which reports the ARM's credential —
    answers a question that module never asked. It must pass the provider it pins."""
    known = ("bedrock", "anthropic", "openai")
    offenders = []
    for path in sorted(_EXTERNAL_TESTS.glob("test_*.py")):
        text = path.read_text(encoding="utf-8")
        if "_live_llm_ready" not in text:
            continue
        # Only an ASSIGNMENT counts as a pin. A bare substring scan also matches prose in a
        # module docstring — test_pydantic_ai_cutover_live.py discusses "anthropic:claude-..."
        # in its history note without pinning anything.
        pinned = {
            p
            for p in known
            if re.search(rf'^\s*\w+\s*=\s*f?["\']{p}:', text, re.MULTILINE)
            or re.search(rf'^\s*\w+\s*=\s*f?["\']\{{[^}}]*\}}{p}:', text, re.MULTILINE)
        }
        if not pinned:
            continue
        if re.search(r"live_llm_ready\(\s*\)", text):
            offenders.append(f"{path.name} (pins {sorted(pinned)})")
    assert not offenders, (
        f"these external modules pin a provider in a model string but ask the readiness probe "
        f"about the ARM's provider instead — on a mismatched arm they report ready and then "
        f"call a provider that arm holds no credential for: {offenders}"
    )


# ── the live tier reports the arm's provider FAMILY, not a protocol qualifier ──────────
@pytest.mark.parametrize("arm", _arms(), ids=lambda a: str(a["provider"]))
def test_every_arm_reports_its_declared_provider_family(
    arm: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    _clean_config_env: None,
) -> None:
    """Bug cb46: ticket 1d22 renamed the openai model qualifier to `openai-chat` and updated
    THIS module's `_MODEL_QUALIFIER_BY_PROVIDER`, but not the live tier — so on the openai arm
    `configured_provider()` reported the QUALIFIER, the credential map missed, every llm_live
    test skipped, and the arm-equality guard in `test_provider_matrix_live.py` false-positived.
    The workflow declares the FAMILY (`matrix.provider` -> REBAR_EXPECTED_LLM_PROVIDER), so the
    live tier's reported provider must be the family name for EVERY arm."""
    mod = _live_llm_module()
    monkeypatch.setenv("REBAR_LLM_CONFIG_FILE", str(_ROOT / arm["config_file"]))
    root_cfg.reset_config_cache()

    reported = mod.configured_provider(str(_ROOT))
    assert reported == arm["provider"], (
        f"overlay {arm['config_file']} makes the live tier report provider {reported!r}, but the "
        f"workflow declares this arm as {arm['provider']!r} — the arm-equality guard and the "
        f"credential map both key on the family name, so a protocol qualifier leaking through "
        f"skips the whole arm"
    )


def test_the_openai_arm_is_live_ready_with_its_own_credential(
    monkeypatch: pytest.MonkeyPatch,
    _clean_config_env: None,
) -> None:
    """The user-visible half of bug cb46: with the openai overlay active and OPENAI_API_KEY
    present, the readiness probe must say READY. While `configured_provider()` returned
    `openai-chat`, `credential_present()` looked up a key that env var name doesn't exist
    under, `live_llm_ready()` was False, and the live openai arm silently skipped everything."""
    mod = _live_llm_module()
    monkeypatch.setenv("REBAR_LLM_CONFIG_FILE", str(_ROOT / ".github/llm-providers/openai.toml"))
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-used-for-any-call")
    for foreign in ("ANTHROPIC_API_KEY", "REBAR_LLM_API_KEY"):
        monkeypatch.delenv(foreign, raising=False)
    monkeypatch.setattr(mod, "agents_extra_installed", lambda: True)
    root_cfg.reset_config_cache()

    assert mod.live_llm_ready() is True, (
        "the openai arm carries OPENAI_API_KEY yet the probe reports not-ready — every "
        f"llm_live test would skip and the arm validates nothing (reason: "
        f"{mod._skip_reason()!r})"
    )
