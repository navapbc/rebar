"""Project defaults resolve LLM model classes and the bare cfg.model through Bedrock. Hermetic
tests read committed configuration and compare project IDs with the CI Bedrock overlay without
credentials, network, or model calls."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import tomllib

from rebar.llm.config import LLMConfig
from rebar.llm.model_classes import (
    CLASS_NAMES,
    load_class_slots,
    parse_class_slots,
    resolve_class,
    resolve_fallback_chain,
)

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]
_REBAR_TOML = _REPO / "rebar.toml"
_BEDROCK_TOML = _REPO / ".github" / "llm-providers" / "bedrock.toml"
_ANTHROPIC_TOML = _REPO / ".github" / "llm-providers" / "anthropic.toml"

# Every variable that outranks the discovered project file, spelled out so a clean-environment
# test is genuinely clean. `REBAR_LLM_CONFIG_FILE` layers OVER the project table;
# `REBAR_LLM_MODEL` is the deprecated bare knob that fans out to all three classes; the nine
# `REBAR_LLM_<CLASS>_<FIELD>` slots are the per-class env layer.
_OUTRANKING_ENV_VARS = (
    "REBAR_LLM_CONFIG_FILE",
    "REBAR_LLM_MODEL",
    *(
        f"REBAR_LLM_{cls.upper()}_{fld}"
        for cls in CLASS_NAMES
        for fld in ("MODEL", "PROVIDER", "ENDPOINT")
    ),
)


def _clean_llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every layer above the discovered project config."""
    for name in _OUTRANKING_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    # A belt-and-braces sweep: any other `REBAR_LLM_*` the ambient shell exports must not decide
    # the outcome of a test about what the FILE says.
    for name in [n for n in os.environ if n.startswith("REBAR_LLM_")]:
        monkeypatch.delenv(name, raising=False)


def _class_models(path: Path) -> dict[str, str]:
    """The `[llm.model_classes]` `model` value per class, as written in `path`."""
    table = tomllib.loads(path.read_text())["llm"]["model_classes"]
    return {cls: str(slot["model"]) for cls, slot in table.items()}


def _resolved_chains(path: Path) -> dict[str, list[str]]:
    """The ordered primary + fallback targets resolved from ``path``."""
    table = tomllib.loads(path.read_text())
    slots = load_class_slots(str(_REPO)) if path == _REBAR_TOML else None
    if slots is None:
        slots = parse_class_slots(table["llm"]["model_classes"])
    return {
        cls: [resolve_class(cls, slots), *resolve_fallback_chain(cls, slots)] for cls in CLASS_NAMES
    }


def test_clean_checkout_resolves_every_class_to_bedrock(monkeypatch: pytest.MonkeyPatch) -> None:
    """No LLM env at all → all three classes are Bedrock inference profiles.

    Reads `rebar.toml` through the real discovery walk by pinning `repo_root` at the repository
    root, so this exercises the same resolution a developer's local gate run performs rather
    than a hand-built table.
    """
    _clean_llm_env(monkeypatch)

    slots = load_class_slots(str(_REPO))
    resolved = {cls: resolve_class(cls, slots) for cls in CLASS_NAMES}

    # The provider claim is asserted FIRST and on its own, so a missing/ignored project table
    # fails on the property that matters rather than on reading the file back.
    for cls, target in resolved.items():
        # Split on the FIRST colon only — the haiku profile id itself ends in `:0`.
        assert target.split(":", 1)[0] == "bedrock", (
            f"class {cls!r} resolves to {target!r}, not a bedrock target. With no REBAR_LLM_* "
            "env set this is what every local gate run uses; an unqualified built-in default "
            "here means the project config's [llm.model_classes] table is missing or ignored."
        )
    assert resolved == _class_models(_REBAR_TOML), (
        "the resolved classes and rebar.toml's [llm.model_classes] disagree, so the project "
        f"default is not the file's: resolved {resolved}"
    )


def test_clean_checkout_resolves_the_bare_cfg_model_to_bedrock_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bare cfg.model resolution must select Bedrock from project configuration, even when class
    lookup is bypassed."""
    _clean_llm_env(monkeypatch)
    # Pin repo_root to this checkout because autouse fixtures redirect REBAR_ROOT to a sandbox.
    model = LLMConfig.from_env(repo_root=str(_REPO)).model

    assert model.split(":", 1)[0] == "bedrock", (
        f"cfg.model resolved to {model!r}. `[llm] model` in rebar.toml is a companion to "
        "[llm.model_classes], not a substitute for it — both are required."
    )


def test_project_default_ids_are_byte_equal_to_the_bedrock_overlay() -> None:
    """Project and CI overlay model-class sets and IDs must match byte-for-byte. The overlay is the
    CI source of provider IDs."""
    project = _class_models(_REBAR_TOML)
    overlay = _class_models(_BEDROCK_TOML)

    assert set(project) == set(overlay), (
        "rebar.toml and .github/llm-providers/bedrock.toml disagree on WHICH model classes "
        f"exist: {sorted(project)} vs {sorted(overlay)}"
    )
    for cls in sorted(overlay):
        assert project[cls] == overlay[cls], (
            f"rebar.toml and .github/llm-providers/bedrock.toml disagree for class {cls!r}: "
            f"the project config has {project[cls]!r}, the overlay has {overlay[cls]!r}. "
            "These are single-sourced deliberately — update BOTH."
        )

    project_scalar = tomllib.loads(_REBAR_TOML.read_text())["llm"]["model"]
    overlay_scalar = tomllib.loads(_BEDROCK_TOML.read_text())["llm"]["model"]
    assert project_scalar == overlay_scalar, (
        f"rebar.toml's `[llm] model` is {project_scalar!r} but the overlay's is "
        f"{overlay_scalar!r}; the second resolution path must name the same frontier id."
    )


def test_project_fallback_ids_are_pinned_to_the_anthropic_overlay() -> None:
    """The enabled direct fallbacks use the exact corresponding matrix-overlay ids.

    Frontier and standard are the measured fallback classes. Trivial deliberately remains a
    single Bedrock arm until direct Haiku native-output parity is measured.
    """
    project_chains = _resolved_chains(_REBAR_TOML)
    anthropic = _class_models(_ANTHROPIC_TOML)

    configured = {cls: chain[1:] for cls, chain in project_chains.items() if len(chain) > 1}
    assert set(configured) == {"frontier", "standard"}
    assert configured == {
        "frontier": [anthropic["frontier"]],
        "standard": [anthropic["standard"]],
    }


def test_clean_checkout_resolves_the_approved_candidate_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bedrock remains primary, direct Anthropic is next, and trivial has no fallback."""
    _clean_llm_env(monkeypatch)

    project = _resolved_chains(_REBAR_TOML)
    bedrock = _class_models(_BEDROCK_TOML)
    anthropic = _class_models(_ANTHROPIC_TOML)

    assert project == {
        "frontier": [bedrock["frontier"], anthropic["frontier"]],
        "standard": [bedrock["standard"], anthropic["standard"]],
        "trivial": [bedrock["trivial"]],
    }


def test_documented_opt_out_returns_resolution_to_anthropic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The escape hatch works: the anthropic overlay outranks the project default.

    This is the mitigation for the onboarding cost of the flip — a contributor without Bedrock
    access, or offline, sets one variable. `REBAR_LLM_CONFIG_FILE` is layered OUTSIDE the
    discovered-config read, which is why it wins over `rebar.toml`.
    """
    _clean_llm_env(monkeypatch)
    monkeypatch.setenv("REBAR_LLM_CONFIG_FILE", str(_ANTHROPIC_TOML))

    slots = load_class_slots(str(_REPO))
    resolved = {cls: resolve_class(cls, slots) for cls in CLASS_NAMES}

    assert resolved == _class_models(_ANTHROPIC_TOML), (
        "pointing REBAR_LLM_CONFIG_FILE at the anthropic overlay did not restore the anthropic "
        f"ids, so the documented opt-out is broken: resolved {resolved}"
    )
    for cls, target in resolved.items():
        assert target.split(":", 1)[0] == "anthropic", (
            f"class {cls!r} resolved to {target!r} under the anthropic opt-out overlay"
        )
