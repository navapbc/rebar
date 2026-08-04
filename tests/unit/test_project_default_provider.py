"""Contract tests for the project's DEFAULT LLM provider being AWS Bedrock (bug d2ce-36f5).

Hermetic: no AWS, no network, no LLM call — these read configuration only.

The decision these pin (ticket d2ce-36f5-fd08-4e40, epic 061c-ecd1): this project's
authoritative config, `rebar.toml`, names Bedrock for all three model classes, so a clean
checkout with no LLM environment set resolves to Bedrock rather than direct Anthropic. Before
this, `rebar.toml` carried no `[llm]` table at all and the built-in class defaults — which are
unqualified names inferring provider `anthropic` — applied, so every local gate ran on direct
Anthropic while the production review bot and the CI matrix ran on Bedrock.

Three properties, one per test:

1. CLEAN RESOLUTION — with no `REBAR_LLM_*` variable and no `REBAR_LLM_CONFIG_FILE`, the three
   classes resolve to `bedrock:` ids. This is the inverse of the measurement that found the gap,
   and it is what makes the default a default rather than a documented opt-in.
2. A TWO-SIDED PIN against `.github/llm-providers/bedrock.toml`, which is the authoritative id
   source shared with the CI provider matrix and the production bot. Exact class-set equality
   plus byte-equal values, so editing EITHER file alone fails; a one-sided "rebar.toml's id
   appears in the overlay" check would pass while the two drifted.
3. THE OPT-OUT — the documented escape hatch (`REBAR_LLM_CONFIG_FILE` at the anthropic overlay)
   returns resolution to `anthropic:`. Flipping a project default imposes an onboarding cost on
   a contributor without Bedrock access, so the mitigation must be exercised, not just written
   down.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import tomllib

from rebar.llm.config import LLMConfig
from rebar.llm.model_classes import CLASS_NAMES, load_class_slots, resolve_class

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
    """`cfg.model` is a SECOND resolution path, and it must not leak to Anthropic.

    Ops that resolve `LLMConfig.model` instead of naming a model class bypass the class table
    entirely and fall back to `DEFAULT_MODEL`, a bare literal that infers provider `anthropic`.
    That ambient-default leak was MEASURED on a live provider-matrix run and closed for the CI
    overlays by [rebar:f124-f14a-fd62-4973]; `[llm] model` in `rebar.toml` closes it for local
    dev, and this asserts it stays closed.
    """
    _clean_llm_env(monkeypatch)

    # `repo_root` is pinned rather than inherited from the cwd: the suite's autouse fixtures
    # point `REBAR_ROOT` at a sandbox repo, so an unpinned read would never see this project's
    # `rebar.toml` at all and the test would assert nothing about it.
    model = LLMConfig.from_env(repo_root=str(_REPO)).model

    assert model.split(":", 1)[0] == "bedrock", (
        f"cfg.model resolved to {model!r}. `[llm] model` in rebar.toml is a companion to "
        "[llm.model_classes], not a substitute for it — both are required."
    )


def test_project_default_ids_are_byte_equal_to_the_bedrock_overlay() -> None:
    """`rebar.toml` and `.github/llm-providers/bedrock.toml` name the SAME ids, from one source.

    Two-sided on purpose: the class SETS must match exactly and each value must be byte-equal,
    so editing either file alone fails. The overlay is the authoritative id source — it is what
    the CI provider matrix and the production review bot resolve — and only inference-profile
    ids are invokable, with non-uniform suffixes, so a hand-typed id here would fail only at
    call time.
    """
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
