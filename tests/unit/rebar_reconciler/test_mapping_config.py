"""Provider-neutral mapping-config core (S1, epic ravenous-dirt-widgeon / bfe7).

The walking-skeleton foundation every other mapping child stands on: a reserved
``[tool.rebar.mapping]`` config section, a three-layer per-key merge (built-in default
<- ``default`` block <- ``projects.<KEY>`` overlay) with wholesale vocabulary
replacement, fail-closed offline validation, and a ``Capability`` descriptor.

Assertions target OBSERVABLE behaviour and contracts only — resolved mapping values,
raised error types, and the module's provider-neutrality — never internal structure.
No axis is wired to the reconciler yet (S2-S5 do that); this pins the core alone.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rebar import config as cfg
from rebar._config_coercion import ConfigError
from rebar_reconciler import mapping_config as mc

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate config discovery: an empty XDG user dir so no real user config leaks a
    ``[mapping]`` section into these tests, and a clean unknown-keys default."""
    xdg = tmp_path / "xdg"
    (xdg / "rebar").mkdir(parents=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    for name in ("REBAR_CONFIG", "REBAR_ROOT", "REBAR_CONFIG_UNKNOWN_KEYS"):
        monkeypatch.delenv(name, raising=False)
    cfg.set_cli_overrides(None)
    cfg.reset_config_cache()


def _proj(tmp: Path, mapping_toml: str = "") -> Path:
    """A repo root whose discovered ``rebar.toml`` carries ``mapping_toml`` verbatim."""
    p = tmp / "proj"
    p.mkdir(parents=True)
    (p / ".git").mkdir()
    (p / "rebar.toml").write_text(mapping_toml, encoding="utf-8")
    cfg.reset_config_cache()
    return p


def _builtin() -> mc.MappingLayer:
    """A provider-NEUTRAL built-in default layer, defined here in the test (never
    imported from an adapter) so these tests carry no vendor coupling. Values are
    deliberately synthetic tokens, not real Jira names."""
    return mc.MappingLayer(
        status_map={"open": "BUILTIN_OPEN", "closed": "BUILTIN_CLOSED"},
        type_map={"story": "BUILTIN_STORY"},
        link_map={"blocks": "BUILTIN_BLOCKS"},
        priority_map={"2": "BUILTIN_MED"},
        create_defaults={},
        statuses=("BUILTIN_OPEN", "BUILTIN_CLOSED"),
        issue_types=("BUILTIN_STORY",),
        link_types=("BUILTIN_BLOCKS",),
        hierarchy={"BUILTIN_EPIC": 1},
    )


# ---------------------------------------------------------------------------
# HAPPY PATH — well-formed input, correct behaviour
# ---------------------------------------------------------------------------


def test_reserved_section_not_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``[mapping]`` is a reserved section: a full strict config load does NOT trip the
    unknown-key error even under ``REBAR_CONFIG_UNKNOWN_KEYS=error``, and the section is
    returned raw by ``read_reserved_section`` — while a genuinely unknown section still
    hard-errors (the negative control that proves strictness is actually active)."""
    monkeypatch.setenv("REBAR_CONFIG_UNKNOWN_KEYS", "error")

    proj = _proj(
        tmp_path,
        '[mapping.projects.REB.status_map]\nopen = "To Do"\n',
    )
    # Reserved: strict load tolerates it.
    cfg.load_config(proj)
    # And it is readable raw, unchanged.
    raw = cfg.read_reserved_section("mapping", proj)
    assert raw["projects"]["REB"]["status_map"]["open"] == "To Do"

    # Negative control: an unknown section DOES hard-error under the same env, proving
    # the tolerance above is reserved-section-specific, not strictness being off.
    bad = _proj(tmp_path / "bad", "[not_a_real_section]\nx = 1\n")
    with pytest.raises(ConfigError):
        cfg.load_config(bad)


def test_perkey_merge_precedence(tmp_path: Path) -> None:
    """Axis maps deep-merge PER KEY: built-in <- ``default`` <- ``projects.<KEY>``. An
    overlay entry overrides only the key it names; unnamed keys inherit the next-outer
    layer."""
    proj = _proj(
        tmp_path,
        "[mapping.default.status_map]\n"
        'open = "DEFAULT_OPEN"\n'
        'in_progress = "DEFAULT_IP"\n'
        "[mapping.projects.REB.status_map]\n"
        'open = "PROJECT_OPEN"\n',
    )
    config = mc.load_mapping_config(proj)
    resolved = mc.resolve_for_project(config, "REB", builtin=_builtin())

    # project layer wins for the key it names
    assert resolved.status_map["open"] == "PROJECT_OPEN"
    # default layer wins over built-in for a key only it names
    assert resolved.status_map["in_progress"] == "DEFAULT_IP"
    # a key named in NO overlay inherits the built-in default
    assert resolved.status_map["closed"] == "BUILTIN_CLOSED"


def test_vocab_lists_replace_wholesale(tmp_path: Path) -> None:
    """Vocabulary declarations REPLACE wholesale (most-specific wins), never union: a
    project's declared ``statuses`` fully supersedes ``default``'s, and a layer that
    declares no vocabulary inherits the next-outer declaration."""
    proj = _proj(
        tmp_path,
        "[mapping.default]\n"
        'statuses = ["D1", "D2", "D3"]\n'
        "[mapping.projects.REB]\n"
        'statuses = ["P1", "P2"]\n'
        "[mapping.projects.OTHER.status_map]\n"
        'open = "x"\n',
    )
    config = mc.load_mapping_config(proj)
    builtin = _builtin()

    # REB declares its own vocabulary: wholesale replacement, NOT a union with default.
    reb = mc.resolve_for_project(config, "REB", builtin=builtin)
    assert set(reb.statuses) == {"P1", "P2"}

    # OTHER declares no vocabulary: it inherits the default block's declaration.
    other = mc.resolve_for_project(config, "OTHER", builtin=builtin)
    assert set(other.statuses) == {"D1", "D2", "D3"}


def test_config_effect_contrast(tmp_path: Path) -> None:
    """A non-default overlay yields a DIFFERENT effective mapping than the built-in
    default, through the real load+resolve path — the read-but-miswired guard."""
    proj = _proj(
        tmp_path,
        '[mapping.projects.REB.status_map]\nopen = "OVERLAID_OPEN"\n',
    )
    config = mc.load_mapping_config(proj)
    builtin = _builtin()

    # A project with no overlay resolves to the built-in value...
    baseline = mc.resolve_for_project(config, "UNCONFIGURED", builtin=builtin)
    # ...while the overlaid project resolves to a different, config-driven value.
    overlaid = mc.resolve_for_project(config, "REB", builtin=builtin)

    assert baseline.status_map["open"] == "BUILTIN_OPEN"
    assert overlaid.status_map["open"] == "OVERLAID_OPEN"
    assert baseline.status_map["open"] != overlaid.status_map["open"]


# ---------------------------------------------------------------------------
# EDGE / fail-closed / neutrality
# ---------------------------------------------------------------------------


def test_validate_fails_closed(tmp_path: Path) -> None:
    """``validate`` fails closed OFFLINE with ``MappingConfigError`` on: a map value
    outside the effective vocabulary, a malformed block (non-mapping sub-table,
    non-string map value, non-string-list vocabulary, non-integer hierarchy), and a
    capability-absent axis reference. The nearest valid config must NOT raise."""
    full_cap = mc.Capability()  # every axis present

    # (a) map value outside the declared/effective vocabulary.
    outside = mc.MappingLayer(
        status_map={"open": "NOT_IN_VOCAB"},
        statuses=("To Do", "Done"),
    )
    with pytest.raises(mc.MappingConfigError):
        mc.validate(outside, full_cap)

    # (b) capability-absent axis reference: a type_map on a target with no types.
    typed = mc.MappingLayer(type_map={"story": "Story"}, issue_types=("Story",))
    no_types = mc.Capability(has_types=False)
    with pytest.raises(mc.MappingConfigError):
        mc.validate(typed, no_types)

    # (c) malformed blocks raise at LOAD (fail-closed on the config load path).
    for bad_toml in (
        '[mapping.default]\nstatus_map = "not-a-table"\n',
        '[mapping.default]\nstatuses = "not-a-list"\n',
    ):
        proj = _proj(tmp_path / f"m{hash(bad_toml) & 0xFFFF}", bad_toml)
        with pytest.raises(mc.MappingConfigError):
            mc.load_mapping_config(proj)

    proj_val = _proj(tmp_path / "mval", "[mapping.default.status_map]\nopen = 5\n")
    with pytest.raises(mc.MappingConfigError):
        mc.load_mapping_config(proj_val)
    proj_h = _proj(tmp_path / "mh", '[mapping.default.hierarchy]\nEpic = "one"\n')
    with pytest.raises(mc.MappingConfigError):
        mc.load_mapping_config(proj_h)

    # NEGATIVE CONTROL: the nearest VALID config validates cleanly (no raise).
    ok = mc.MappingLayer(
        status_map={"open": "To Do"},
        type_map={"story": "Story"},
        statuses=("To Do", "Done"),
        issue_types=("Story",),
    )
    mc.validate(ok, full_cap)  # must not raise

    # And the reserved SKIP sentinel is always an allowed map value.
    skipped = mc.MappingLayer(
        link_map={"supersedes": mc.SKIP},
        link_types=("Blocks",),
    )
    mc.validate(skipped, full_cap)  # must not raise


def test_core_is_provider_neutral() -> None:
    """The core carries no Jira coupling: its source imports no ``adapters.jira*``
    module and contains no Jira value literal — the structural neutrality guarantee
    (Jira specifics live only in the adapter, which injects them at the call site)."""
    source = Path(mc.__file__).read_text(encoding="utf-8")

    assert "adapters.jira" not in source
    assert "adapters/jira" not in source

    jira_value_literals = (
        "To Do",
        "In Progress",
        "IDEA",
        "Highest",
        "Lowest",
        "Blocks",
        "Relates",
        '"Story"',
        '"Epic"',
        '"Bug"',
        '"Task"',
    )
    present = [lit for lit in jira_value_literals if lit in source]
    assert present == [], f"provider-neutral core must carry no Jira value literal: {present}"
