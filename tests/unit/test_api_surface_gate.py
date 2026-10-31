"""Portable regression tests for the public Python API baseline.

``scripts/gen_api_surface.py`` snapshots the ``rebar`` facade and the reuse subsystems
documented in ``docs/reuse-surface.md`` into ``api_surface_baseline.json``. These tests run
under ``make test`` without a CI provider and reject removed or renamed symbols, signature
changes, class-shape changes, and public-constant changes.

The baseline subsumes the curated signature assertions in ``test_reuse_surface_doc.py``.
Subsumption tests retain coverage of signing, prompt-library, runner-contract, and
workflow-executor APIs.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import inspect
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from _nested_pytest import run_nested_pytest
from _subprocess_env import SubprocessEnv, subprocess_env

pytestmark = [
    pytest.mark.unit,
    pytest.mark.allow_unharnessed_subprocess(
        "every test runs the API-surface gate in a clean interpreter rooted at the\n"
        "committed tree, deliberately escaping the unit tier's autouse monkeypatches"
    ),
]

REPO_ROOT = Path(__file__).resolve().parents[2]
GEN_PATH = REPO_ROOT / "scripts" / "gen_api_surface.py"


def _load():
    spec = importlib.util.spec_from_file_location("gen_api_surface", GEN_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gen = _load()


@pytest.fixture
def redirect_baseline(tmp_path, monkeypatch):
    """Point the generator's baseline path at a tmp file so no committed file is mutated."""
    baseline = tmp_path / "api_surface_baseline.json"
    monkeypatch.setattr(gen, "BASELINE_PATH", baseline)
    return baseline


# ─────────────────────────── the portable gate itself ────────────────────────


def _run_gate(*args: str, env: SubprocessEnv | None = None) -> subprocess.CompletedProcess[str]:
    """Run the gate script in a CLEAN subprocess.

    A subprocess imports ``rebar`` in a fresh interpreter, so the check reflects the
    *real* installed public surface — immune to the unit tier's autouse monkeypatches
    (e.g. ``_no_real_session_log_writes`` replaces ``rebar.append_session_log`` in-process).
    It uses only ``sys.executable`` + stdlib, so it stays portable with no CI dependency.
    """
    return subprocess.run(
        [sys.executable, str(GEN_PATH), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def test_committed_baseline_matches_live_surface():
    """The committed baseline is current: ``--check`` exits 0 in a clean interpreter."""
    result = _run_gate("--check")
    assert result.returncode == 0, result.stderr


def test_default_invocation_runs_the_check():
    """A bare run defaults to ``--check`` and passes against the committed baseline."""
    assert _run_gate().returncode == 0


def test_baseline_file_is_committed_and_wellformed():
    """The baseline exists, is valid JSON, and pins every module in ``MODULES``."""
    data = json.loads(gen.BASELINE_PATH.read_text(encoding="utf-8"))
    for module in gen.MODULES:
        assert module in data, f"baseline missing pinned module {module!r}"


# ── RED→GREEN: the gate catches an injected surface drift (both directions) ────


def test_check_trips_when_a_public_symbol_is_removed(monkeypatch):
    """Dropping a public symbol from the live surface makes ``--check`` fail and name it."""
    baseline = gen.build_surface()

    def _drifted():
        surface = json.loads(json.dumps(baseline))
        surface["rebar"].pop("claim")
        return surface

    monkeypatch.setattr(gen, "build_surface", _drifted)
    monkeypatch.setattr(gen, "_load_baseline", lambda: baseline)
    assert gen.main(["--check"]) == 1
    drift = gen.diff_surface(baseline, _drifted())
    assert any(line == "- REMOVED  rebar.claim" for line in drift)


def test_check_trips_when_a_public_symbol_is_added(monkeypatch):
    """Adding a public symbol makes ``--check`` fail (the surface grew without a baseline)."""
    baseline = gen.build_surface()

    def _drifted():
        surface = json.loads(json.dumps(baseline))
        surface["rebar"]["brand_new_public_thing"] = {"kind": "callable", "params": []}
        return surface

    monkeypatch.setattr(gen, "build_surface", _drifted)
    monkeypatch.setattr(gen, "_load_baseline", lambda: baseline)
    assert gen.main(["--check"]) == 1
    assert "+ ADDED    rebar.brand_new_public_thing" in gen.diff_surface(baseline, _drifted())


def test_check_trips_when_a_signature_changes(monkeypatch):
    """Renaming a parameter (a breaking signature change) is caught as CHANGED."""
    baseline = gen.build_surface()

    def _drifted():
        surface = json.loads(json.dumps(baseline))
        params = surface["rebar.signing"]["sign_manifest"]["params"]
        params[0][0] = "renamed_first_arg"
        return surface

    monkeypatch.setattr(gen, "build_surface", _drifted)
    monkeypatch.setattr(gen, "_load_baseline", lambda: baseline)
    assert gen.main(["--check"]) == 1
    assert "~ CHANGED  rebar.signing.sign_manifest" in gen.diff_surface(baseline, _drifted())


def test_callable_descriptor_includes_normalized_return_annotation():
    class Widget:
        pass

    def no_annotation(value: int):
        return value

    def none_annotation(value: int) -> None:
        return None

    def string_annotation(value: int) -> Widget:
        return value

    def live_annotation(value: int):
        return Path(str(value))

    live_annotation.__annotations__["return"] = Path

    assert gen._describe(no_annotation) == {
        "kind": "callable",
        "params": [
            ["value", "POSITIONAL_OR_KEYWORD", "<none>"],
        ],
        "return": None,
    }
    assert gen._describe(none_annotation)["return"] == "None"
    assert gen._describe(string_annotation)["return"] == "Widget"
    assert gen._describe(live_annotation)["return"] == inspect.formatannotation(
        inspect.signature(live_annotation).return_annotation
    )


def test_captured_class_methods_include_normalized_return_annotations():
    class Widget:
        pass

    class Example:
        def method(self, value: int) -> Widget:
            return value

    Example.__module__ = "rebar.synthetic"
    Example.method.__module__ = "rebar.synthetic"

    assert gen._describe_class(Example)["methods"]["method"] == {
        "params": [
            ["self", "POSITIONAL_OR_KEYWORD", "<none>"],
            ["value", "POSITIONAL_OR_KEYWORD", "<none>"],
        ],
        "return": "Widget",
    }


def test_return_only_drift_is_detected_for_top_level_callable(monkeypatch):
    baseline = {
        "rebar": {
            "sample": {
                "kind": "callable",
                "params": [["value", "POSITIONAL_OR_KEYWORD", "<none>"]],
                "return": "OldReturn",
            }
        }
    }

    def _drifted():
        surface = json.loads(json.dumps(baseline))
        surface["rebar"]["sample"]["return"] = "NewReturn"
        return surface

    monkeypatch.setattr(gen, "build_surface", _drifted)
    monkeypatch.setattr(gen, "_load_baseline", lambda: baseline)
    assert gen.main(["--check"]) == 1
    assert "~ CHANGED  rebar.sample" in gen.diff_surface(baseline, _drifted())


def test_return_only_drift_is_detected_for_captured_class_method(monkeypatch):
    baseline = {
        "rebar": {
            "Example": {
                "kind": "class",
                "bases": [],
                "methods": {
                    "method": {
                        "params": [["self", "POSITIONAL_OR_KEYWORD", "<none>"]],
                        "return": "OldReturn",
                    }
                },
            }
        }
    }

    def _drifted():
        surface = json.loads(json.dumps(baseline))
        surface["rebar"]["Example"]["methods"]["method"]["return"] = "NewReturn"
        return surface

    monkeypatch.setattr(gen, "build_surface", _drifted)
    monkeypatch.setattr(gen, "_load_baseline", lambda: baseline)
    assert gen.main(["--check"]) == 1
    assert "~ CHANGED  rebar.Example" in gen.diff_surface(baseline, _drifted())


def test_no_drift_reported_against_self():
    """An identical surface produces an empty diff (no false positives)."""
    surface = gen.build_surface()
    assert gen.diff_surface(surface, surface) == []


# ── the documented baseline-update path, exercised end to end ─────────────────


def test_update_then_check_is_clean(redirect_baseline):
    """``--update`` writes the current surface; a subsequent ``--check`` is clean (exit 0)."""
    assert not redirect_baseline.exists()
    assert gen.main(["--update"]) == 0
    assert redirect_baseline.exists()
    assert gen.main(["--check"]) == 0


def test_intentional_change_is_accommodated_by_updating_the_baseline(
    redirect_baseline, monkeypatch
):
    """A stale baseline trips ``--check``; re-running ``--update`` accepts the change.

    This is the documented remediation for an *intentional* surface change: the gate
    fails loudly, the author runs ``gen_api_surface.py --update``, and the gate passes.
    """
    stale = gen.build_surface()
    stale["rebar"].pop("claim")
    redirect_baseline.write_text(
        json.dumps(stale, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    # The removed `claim` is still live, so the stale baseline is detected as drift.
    assert gen.main(["--check"]) == 1

    # The sanctioned update step re-syncs the baseline and clears the gate.
    assert gen.main(["--update"]) == 0
    assert gen.main(["--check"]) == 0


def test_missing_baseline_reports_full_surface_as_drift(redirect_baseline):
    """With no committed baseline, every symbol reads as ADDED and ``--check`` fails."""
    assert not redirect_baseline.exists()
    assert gen.main(["--check"]) == 1


# ── a stale editable install (bug 9cee-46d4-a822-43c0) ────────────────────────
#
# An editable install writes its version into .dist-info once. A rebase across a version
# bump leaves that metadata behind, so rebar.__version__ reports the old version while
# pyproject.toml declares the new one, and the surface gate reads the gap as API drift.

STALE_VERSION = "0.0.1"


@pytest.fixture
def stale_install_env(tmp_path) -> SubprocessEnv:
    """An env whose ``PYTHONPATH`` shadows the real dist-info with an older version.

    ``PYTHONPATH`` precedes site-packages, so ``importlib.metadata`` finds this
    distribution first while ``import rebar`` still loads the checkout's ``src`` — the
    same split a rebased editable install has, without touching the real venv.
    """
    shadow = tmp_path / "shadow"
    dist_info = shadow / f"nava_rebar-{STALE_VERSION}.dist-info"
    dist_info.mkdir(parents=True)
    (dist_info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: nava-rebar\nVersion: {STALE_VERSION}\n", encoding="utf-8"
    )
    env = subprocess_env()
    env.pop("PYTEST_ADDOPTS", None)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(shadow), env.get("PYTHONPATH")) if p)
    return env


def test_version_drift_names_both_versions_and_the_remedy():
    message = gen.installed_version_drift(installed="0.13.1", declared="0.14.0")
    assert message is not None
    assert "0.13.1" in message and "0.14.0" in message
    assert "make install" in message


def test_matching_versions_report_no_drift():
    assert gen.installed_version_drift(installed="0.14.0", declared="0.14.0") is None


def test_an_uninstalled_distribution_reports_no_drift(monkeypatch):
    """A bare source tree has no dist-info to be stale; ``rebar`` falls back to 0+unknown."""

    def _not_installed(name: str) -> str:
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", _not_installed)
    assert gen.installed_version_drift(declared="0.14.0") is None


def test_the_aligned_test_environment_reports_no_drift():
    assert gen.installed_version_drift() is None


def test_a_stale_install_stops_the_pytest_session_before_any_test(stale_install_env, tmp_path):
    result = run_nested_pytest(
        tmp_path / "run",
        "--collect-only",
        "-q",
        str(Path(__file__).resolve()),
        env=stale_install_env,
        timeout=120,
    )
    output = result.stdout + result.stderr
    assert result.returncode == pytest.ExitCode.USAGE_ERROR, output
    assert STALE_VERSION in output and gen.declared_version() in output
    assert "make install" in output


def test_version_only_drift_from_a_stale_install_does_not_advise_update(stale_install_env):
    result = _run_gate("--check", env=stale_install_env)
    assert result.returncode == 1, result.stderr
    assert "~ CHANGED  rebar.__version__" in result.stderr
    assert "make install" in result.stderr
    assert f"{gen.GEN_CMD} --update" not in result.stderr


def test_update_refuses_to_bake_a_stale_install_into_the_baseline(stale_install_env):
    before = gen.BASELINE_PATH.read_bytes()
    try:
        result = _run_gate("--update", env=stale_install_env)
        after = gen.BASELINE_PATH.read_bytes()
    finally:
        gen.BASELINE_PATH.write_bytes(before)  # never leave a corrupted baseline behind
    assert result.returncode == 2, result.stderr
    assert "make install" in result.stderr
    assert after == before


# ── subsumption: the exact subset the old hardcoded test guarded is still guarded ──


def _surface() -> dict:
    return gen.build_surface()


def test_subsumes_signing_signatures():
    """Every signing signature the old test hardcoded is present in the surface."""
    signing = _surface()["rebar.signing"]

    def params(name):
        return [p[0] for p in signing[name]["params"]]

    assert params("sign_manifest") == ["ticket_id", "manifest", "kind", "repo_root", "signer"]
    assert params("verify_signature") == ["ticket_id", "kind", "repo_root"]
    assert params("verify_attestations") == ["ticket_id", "repo_root"]
    assert params("parse_manifest") == ["payload"]
    assert params("head_sha") == ["repo_root"]


def test_subsumes_prompt_library_surface():
    """The prompt-library callables and closed constants the old test checked are guarded."""
    prompts = _surface()["rebar.llm.prompting.prompts"]

    assert [p[0] for p in prompts["get_prompt"]["params"]] == ["prompt_id", "repo_root"]
    assert [p[0] for p in prompts["resolve_prompt"]["params"]] == [
        "reviewer",
        "variables",
        "langfuse_cfg",
        "repo_root",
        "variant",
    ]
    front_matter = prompts["FRONT_MATTER_KEYS"]["value"]
    for key in (
        "schema_version",
        "title",
        "description",
        "inputs",
        "outputs",
        "execution_mode",
        "category",
        "tags",
        "dimension",
        "applies_to",
    ):
        assert repr(key) in front_matter
    assert prompts["EXECUTION_MODES"]["value"] == [repr("single_turn"), repr("agentic")]


def test_subsumes_runner_and_contract_surface():
    """The runner dataclass fields and contract/findings signatures are guarded."""
    surface = _surface()
    fields = surface["rebar.llm.runner"]["RunRequest"]["fields"]
    for f in ("system_prompt", "instructions", "config", "output_schema", "mode", "execution_mode"):
        assert f in fields

    runner = surface["rebar.llm.runner"]
    assert [p[0] for p in runner["get_runner"]["params"]] == ["config", "runtime", "override"]

    contracts = surface["rebar.llm.contracts"]
    assert [p[0] for p in contracts["register_contract"]["params"]] == ["name", "builder"]
    assert [p[0] for p in contracts["response_model_for"]["params"]] == ["output_schema"]

    findings = surface["rebar.llm.findings"]
    assert [p[0] for p in findings["validate_structured"]["params"]] == ["data", "output_schema"]


def test_subsumes_workflow_executor_surface():
    """The ``run_workflow`` parameters the old test checked are guarded."""
    pytest.importorskip("yaml")
    run_workflow = _surface()["rebar.llm.workflow.executor"]["run_workflow"]
    names = [p[0] for p in run_workflow["params"]]
    for p in ("doc", "inputs", "run_id", "target_ticket", "repo_root"):
        assert p in names
