"""Portable drift gate for rebar's public Python API surface (ticket a454-9285-b999-4ac5).

The generator ``scripts/gen_api_surface.py`` snapshots the pinned public surface — the
``rebar`` facade (``rebar.__all__``) plus the reuse subsystems documented in
``docs/reuse-surface.md`` — into the committed baseline
``tests/unit/api_surface_baseline.json``. This test is the portable proving mechanism:
it runs under ``make test`` with **no CI-provider dependency** and fails whenever the live
public surface drifts from that baseline (a removed/renamed symbol, a changed signature,
a changed class shape, or a changed public constant).

It also **subsumes** the former hand-maintained hardcoded signature assertions in
``test_reuse_surface_doc.py`` (ticket f5df): those introspected a curated subset of
signing / prompt-library / runner-contract / workflow-executor signatures by hand; the
baseline now captures the *whole* surface automatically, and the subsumption tests below
assert that exact subset is still guarded — no loss of coverage, no manual lockstep.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

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


def _run_gate(*args: str) -> subprocess.CompletedProcess[str]:
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
    assert params("verify_record") == ["record", "ticket_id", "key"]
    assert params("signing_key") == ["tracker", "create_if_missing"]
    assert params("key_fingerprint") == ["key"]
    assert params("compute_signature") == ["ticket_id", "manifest", "key"]
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
        "default",
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
