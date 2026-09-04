"""Unit tests for the semantic capability registry (RP-05 S4, ``rebar._capabilities``).

The registry is a stdlib-only, descriptive census that maps each *semantic capability*
(``agent_runtime``, ``audit_ui``, …) to its packaging extra and a typed *missing posture*
(``error`` / ``unavailable`` / ``abstain`` / ``fallback``). It is descriptive,
error-shaping infrastructure — it never imports an optional package and never manufactures
a domain result (ADR 0100 §7).

Happy-path oracle: the shipped registry has the expected shape and validates clean.
The edge/boundary and end-to-end contracts live alongside (held out during implementation).
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest
import tomllib

from rebar import _capabilities as cap
from rebar._optional import OptionalDependencyError

# The initial complete runtime census (ticket 448e Approach), key -> (extra, posture).
_EXPECTED: dict[str, tuple[str, str]] = {
    "agent_runtime": ("agents", "error"),
    "audit_ui": ("ui", "error"),
    "bedrock_provider": ("bedrock", "error"),
    "jira_datacenter": ("jira-datacenter", "error"),
    "s3_remote": ("s3", "error"),
    "metrics_lizard": ("metrics", "unavailable"),
    "pricing": ("pricing", "unavailable"),
    "grounding_structural": ("grounding", "abstain"),
    "grounding_t2": ("grounding-t2", "abstain"),
    "grounding_terraform": ("grounding-terraform", "abstain"),
    "cloud_adf": ("adf", "fallback"),
    "datacenter_wiki": ("wiki", "fallback"),
    "trace_export": ("tracing", "fallback"),
    "mcp_server": ("mcp", "error"),
    "review_bot": ("reviewbot", "error"),
}


def test_registry_has_exactly_the_census_keys() -> None:
    assert set(cap.CAPABILITY_KEYS) == set(_EXPECTED)


def test_each_capability_has_expected_extra_and_posture() -> None:
    for key, (extra, posture) in _EXPECTED.items():
        record = cap.get(key)
        assert record.extra == extra, key
        assert record.posture.value == posture, key


def test_shipped_registry_validates_clean() -> None:
    assert cap.validate() == ()


def test_every_capability_declares_a_probe_and_install_hint() -> None:
    for key in cap.CAPABILITY_KEYS:
        record = cap.get(key)
        assert record.probe, key
        assert "pip install" in record.install_hint, key
        assert record.extra in record.install_hint, key


def test_all_four_postures_are_represented() -> None:
    postures = {cap.get(k).posture.value for k in cap.CAPABILITY_KEYS}
    assert postures == {"error", "unavailable", "abstain", "fallback"}


def test_is_available_returns_a_bool() -> None:
    assert isinstance(cap.is_available("metrics_lizard"), bool)


def test_dev_is_not_a_runtime_capability_extra() -> None:
    # ``dev`` is packaging/development metadata, never a runtime capability (ticket Approach).
    assert "dev" not in {cap.get(k).extra for k in cap.CAPABILITY_KEYS}


_PROBE_MODULES = sorted({cap.get(k).probe.split(".")[0] for k in cap.CAPABILITY_KEYS})


# ── validation catches malformed registries ─────────────────────────────────────────────
def test_validate_flags_undeclared_extra() -> None:
    bad = (*cap.CAPABILITIES, replace(cap.get("pricing"), key="rogue", extra="not-an-extra"))
    codes = {f.code for f in cap.validate(bad)}
    assert "undeclared_extra" in codes


def test_validate_flags_duplicate_key() -> None:
    dup = (*cap.CAPABILITIES, replace(cap.get("pricing")))
    codes = {f.code for f in cap.validate((*cap.CAPABILITIES, dup[-1]))}
    assert "duplicate_key" in codes


def test_validate_flags_extra_not_in_declared_set() -> None:
    # Restricting the declared set makes a legitimately-declared extra invalid.
    findings = cap.validate(declared_extras=frozenset({"agents"}))
    assert any(f.code == "undeclared_extra" for f in findings)


# ── the shared error-posture helper ─────────────────────────────────────────────────────
def test_require_capability_raises_optional_dependency_error_when_absent(block_extra) -> None:
    block_extra(cap.get("audit_ui").probe.split(".")[0])
    assert cap.is_available("audit_ui") is False
    with pytest.raises(OptionalDependencyError) as ei:
        cap.require_capability("audit_ui")
    msg = str(ei.value)
    assert "nava-rebar[ui]" in msg
    assert "pip install" in msg


def test_require_capability_names_the_exact_extra_per_capability(block_extra) -> None:
    # bedrock's guidance is the pydantic-ai-slim install, not nava-rebar[bedrock].
    block_extra(cap.get("bedrock_provider").probe.split(".")[0])
    with pytest.raises(OptionalDependencyError) as ei:
        cap.require_capability("bedrock_provider")
    assert "pydantic-ai-slim[bedrock]" in str(ei.value)


def test_require_capability_refuses_non_error_posture() -> None:
    # metrics is an ``unavailable`` posture — the domain owns its result, not this helper.
    with pytest.raises(ValueError):
        cap.require_capability("metrics_lizard")


def test_missing_error_is_optional_dependency_error() -> None:
    err = cap.missing_error("s3_remote")
    assert isinstance(err, OptionalDependencyError)
    assert "nava-rebar[s3]" in str(err)


def test_require_capability_is_noop_when_probe_present(monkeypatch) -> None:
    # Deterministic (env-independent): force an error-posture capability to be "available" and
    # prove the guard is a silent no-op — never gated behind a maybe-absent extra.
    monkeypatch.setattr(cap, "is_available", lambda key: True)
    assert cap.require_capability("audit_ui") is None


def test_optional_delegators_reach_the_semantic_seam(block_extra) -> None:
    # The compatibility delegators on rebar._optional must reach the new capability seam
    # (and their lazy import must not deadlock on the _capabilities <-> _optional cycle).
    from rebar import _optional

    assert isinstance(_optional.capability_installed("metrics_lizard"), bool)
    block_extra(cap.get("audit_ui").probe.split(".")[0])
    with pytest.raises(OptionalDependencyError) as ei:
        _optional.require_capability("audit_ui")
    assert "nava-rebar[ui]" in str(ei.value)


# ── import isolation: no probe package is EXECUTED/imported by the registry ───────────────
def test_registry_never_imports_a_probe_package() -> None:
    # The contract forbids *importing* (executing) an optional probe package, not consulting
    # its spec: ``is_available`` legitimately uses ``importlib.util.find_spec`` (which does not
    # execute the module). So the oracle is: after a fresh import + validate() + a full
    # is_available() sweep, none of the probe packages appear as EXECUTED modules.
    import importlib

    before = set(sys.modules)
    importlib.reload(cap)
    assert cap.validate() == ()
    for key in cap.CAPABILITY_KEYS:
        cap.is_available(key)  # find_spec only — must not execute the package
    newly_executed = set(sys.modules) - before
    leaked = {m for m in newly_executed if m.split(".")[0] in _PROBE_MODULES}
    assert leaked == set(), f"registry executed optional probe package(s): {sorted(leaked)}"


def test_is_available_does_not_leave_probe_in_sys_modules() -> None:
    # is_available uses find_spec, which resolves a spec WITHOUT executing the module — so a
    # not-already-loaded probe must NOT appear in sys.modules afterwards (strong: an executed
    # module would be present).
    probe = cap.get("metrics_lizard").probe.split(".")[0]
    sys.modules.pop(probe, None)
    cap.is_available("metrics_lizard")
    assert probe not in sys.modules


# ── pyproject parity: every extra is a declared packaging extra (minus dev) ──────────────
def _declared_extras() -> set[str]:
    root = Path(__file__).resolve()
    for parent in root.parents:
        pp = parent / "pyproject.toml"
        if pp.exists():
            data = tomllib.loads(pp.read_text())
            extras = set(data["project"]["optional-dependencies"])
            extras.discard("dev")
            return extras
    raise AssertionError("pyproject.toml not found")


def test_every_capability_extra_is_a_declared_packaging_extra() -> None:
    declared = _declared_extras()
    for key in cap.CAPABILITY_KEYS:
        assert cap.get(key).extra in declared, key


def test_declared_extras_constant_matches_pyproject() -> None:
    assert set(cap.DECLARED_EXTRAS) == _declared_extras()


# ── single source: CLI route registry validates against THIS registry ────────────────────
def test_cli_route_registry_capability_names_are_the_semantic_keys() -> None:
    from rebar._cli import _registry

    assert set(_registry.KNOWN_CAPABILITIES) == set(cap.CAPABILITY_KEYS)


def test_route_advertising_a_semantic_capability_is_accepted() -> None:
    from rebar._cli._registry import Route, validate

    route = Route(name="probe", group="reads_no_init", capabilities=("audit_ui",))
    codes = {f.code for f in validate((route,))}
    assert "unknown_capability" not in codes


def test_route_advertising_an_unknown_capability_is_rejected() -> None:
    from rebar._cli._registry import Route, validate

    route = Route(name="probe", group="reads_no_init", capabilities=("no_such_capability",))
    codes = {f.code for f in validate((route,))}
    assert "unknown_capability" in codes


def test_mcp_and_reviewbot_are_capabilities_but_not_cli_routes() -> None:
    from rebar._cli._registry import ROUTES

    route_names = {r.name for r in ROUTES}
    assert "mcp_server" in cap.CAPABILITY_KEYS
    assert "review_bot" in cap.CAPABILITY_KEYS
    # separate entrypoints: never a top-level ``rebar`` verb
    assert "rebar-mcp" not in route_names
    assert "mcp" not in route_names
    assert cap.get("mcp_server").posture.value == "error"
    assert cap.get("review_bot").posture.value == "error"
