"""Unit + schema-conformance tests for the public oracle facade (S5).

Pins the three query surfaces over the three engines, the closed dimension-ID
vocabulary owned here (and consumed by the detector registry), and the static
integration contract emitted by the `grounding-info` read tool (library / CLI /
MCP), validated against the canonical `grounding_info` schema.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import rebar
from rebar import schemas
from rebar.grounding import evidence as ev
from rebar.grounding import oracle
from rebar.grounding.detectors import registry as det_registry

pytestmark = pytest.mark.unit


# ── the closed dimension vocabulary (owned here; registry draws from it) ──────


def test_dimension_vocabulary_is_closed_and_versioned() -> None:
    assert isinstance(oracle.DIMENSIONS, frozenset)
    assert oracle.DIMENSIONS_VERSION >= 1
    # The canonical set carries the load-bearing applicability + the smell catch-all.
    for required in ("web_frontend", "has_iac", "touches_auth", "smell_generic"):
        assert required in oracle.DIMENSIONS


def test_registry_dimensions_mirror_the_canonical_set() -> None:
    # The detector registry's module-level mirror must equal S5's canonical set
    # (the single source of truth), and the lazy accessor must resolve to it.
    assert det_registry.DIMENSIONS == oracle.DIMENSIONS
    assert det_registry._canonical_dimensions() == oracle.DIMENSIONS


def test_reference_kinds_are_exposed_not_redefined() -> None:
    # S5 EXPOSES the S2 reference-in `kind` set; it does not redefine it.
    from rebar.grounding import resolve

    assert oracle.REFERENCE_KINDS is resolve.REFERENCE_KINDS


# ── surface 1: refute_absence routes by kind ─────────────────────────────────


def test_refute_absence_routes_file_kind(tmp_path: Path) -> None:
    (tmp_path / "present.py").write_text("x = 1\n", encoding="utf-8")
    rec = oracle.refute_absence(
        {"kind": "file", "name": "present.py"}, repo_root=str(tmp_path)
    )
    ev.validate(rec)
    assert rec["outcome"] == ev.OUTCOME_REFUTED


@pytest.mark.parametrize(
    "bad_reference",
    [
        {"kind": "bogus", "name": "x"},   # out-of-vocab kind
        {"kind": "file"},                  # missing name
        "not-a-mapping",                   # wrong type entirely
    ],
)
def test_refute_absence_malformed_reference_fails_open(bad_reference, tmp_path: Path) -> None:
    # The facade must ABSORB a malformed reference into an abstain — never raise.
    # (refute_absence is the untrusted-input entry point for 5fd2/9da1.)
    rec = oracle.refute_absence(bad_reference, repo_root=str(tmp_path))
    ev.validate(rec)
    assert rec["outcome"] == ev.OUTCOME_ABSTAIN
    assert rec["reason"] in ev.ABSTAIN_REASONS


def test_refute_absence_routes_dependency_to_deps_lane(monkeypatch, tmp_path: Path) -> None:
    # The unification: kind=dependency must hit the deps lane (not abstain-and-route).
    # Stub the network seam so the probe is deterministic + offline.
    from rebar.grounding import deps

    monkeypatch.setattr(deps, "_http_get", lambda url, **kw: 200)
    rec = oracle.refute_absence(
        {"kind": "dependency", "name": "requests", "ecosystem": "pypi"},
        repo_root=str(tmp_path),
    )
    ev.validate(rec)
    assert rec["outcome"] == ev.OUTCOME_REFUTED
    assert rec["provenance_tier"] == ev.TIER_T0


def test_refute_absence_dependency_not_found_abstains(monkeypatch, tmp_path: Path) -> None:
    from rebar.grounding import deps

    monkeypatch.setattr(deps, "_http_get", lambda url, **kw: 404)
    rec = oracle.refute_absence(
        {"kind": "dependency", "name": "totally-made-up-xyz", "ecosystem": "pypi"},
        repo_root=str(tmp_path),
    )
    ev.validate(rec)
    assert rec["outcome"] == ev.OUTCOME_ABSTAIN  # never an asserted absence


# ── surface 2: applies validates the dimension + returns evidence ─────────────


def test_applies_unknown_dimension_abstains(tmp_path: Path) -> None:
    rec = oracle.applies("not_a_real_dimension", str(tmp_path))
    ev.validate(rec)
    assert rec["outcome"] == ev.OUTCOME_ABSTAIN
    assert rec["reason"] == "invalid_detector"


def test_applies_known_dimension_returns_valid_evidence(tmp_path: Path) -> None:
    # A python-only repo: web_frontend does not apply -> a valid abstain w/ coverage.
    (tmp_path / "main.py").write_text("print('hi')\n", encoding="utf-8")
    rec = oracle.applies("web_frontend", str(tmp_path))
    ev.validate(rec)
    assert rec["job"] == ev.JOB_APPLIES
    assert rec["outcome"] in (ev.OUTCOME_MATCH, ev.OUTCOME_ABSTAIN)


# ── surface 3: scan filters + returns valid evidence ─────────────────────────


def test_scan_returns_valid_evidence(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    records = oracle.scan(str(tmp_path))
    for r in records:
        ev.validate(r)


def test_scan_dimension_filter_narrows(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    all_records = oracle.scan(str(tmp_path))
    smell = oracle.scan(str(tmp_path), dimensions=["smell_generic"])
    # Filtering can only narrow (or equal) the record set.
    assert len(smell) <= len(all_records)
    for r in smell:
        ev.validate(r)


def test_scan_unknown_dimension_yields_nothing(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    assert oracle.scan(str(tmp_path), dimensions=["nope_not_real"]) == []


# ── the static contract + schema conformance (library / CLI / MCP) ────────────


def test_contract_validates_against_grounding_info_schema() -> None:
    pytest.importorskip("jsonschema")
    pytest.importorskip("referencing")
    info = oracle.contract()
    schemas.validator(schemas.GROUNDING_INFO).validate(info)
    # The contract advertises exactly the canonical vocabulary + version.
    assert info["dimensions_version"] == oracle.DIMENSIONS_VERSION
    assert set(info["dimensions"]) == set(oracle.DIMENSIONS)
    assert set(info["reference_kinds"]) == set(oracle.REFERENCE_KINDS)
    assert set(info["abstain_reasons"]) == set(ev.ABSTAIN_REASONS)


def test_library_grounding_info_validates() -> None:
    pytest.importorskip("jsonschema")
    pytest.importorskip("referencing")
    schemas.validator(schemas.GROUNDING_INFO).validate(rebar.grounding_info())


def test_cli_grounding_info_json_validates() -> None:
    pytest.importorskip("jsonschema")
    pytest.importorskip("referencing")
    cp = subprocess.run(
        [sys.executable, "-m", "rebar.cli", "grounding-info", "--output", "json"],
        capture_output=True,
        text=True,
    )
    assert cp.returncode == 0, cp.stderr
    info = json.loads(cp.stdout)
    schemas.validator(schemas.GROUNDING_INFO).validate(info)


def test_cli_grounding_info_text_is_human_readable() -> None:
    cp = subprocess.run(
        [sys.executable, "-m", "rebar.cli", "grounding-info"],
        capture_output=True,
        text=True,
    )
    assert cp.returncode == 0, cp.stderr
    assert "code-grounding oracle contract" in cp.stdout
    assert "dimensions:" in cp.stdout
