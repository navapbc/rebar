"""Happy-path oracle for the config-reference generator (ticket 3199, RP-04 S7.4).

The generator (scripts/gen_config_reference.py) emits TWO new, fully-generated docs:

  * docs/config-reference.md — the machine-current configuration precedence / source /
    lifecycle reference, whose per-key rows are derived from the typed config schema
    (rebar._config_schema) with a LIFECYCLE column derived from the cfg-kind entries of
    rebar._deprecations.REGISTRY + rebar._deprecations.tombstones().
  * docs/security.md — the indexed security reference, whose secret-name inventory is
    derived from rebar._child_env._ADAPTER_SECRET_NAMES.

Narrative topics that are not registry-derivable are emitted verbatim from a curated
template constant in the generator. The existing hand-authored docs/config.md is left
ENTIRELY untouched.

This module is the HAPPY-PATH subset. The edge/E2E oracle (config.md preservation,
lifecycle column, secret inventory, topic coverage, CRLF newline-safety, drift-trips)
is held out in a separate suite the implementer never sees.
"""

from __future__ import annotations

import dataclasses
import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
GEN_PATH = REPO_ROOT / "scripts" / "gen_config_reference.py"
CONFIG_MD = REPO_ROOT / "docs" / "config.md"
CONFIG_REFERENCE = REPO_ROOT / "docs" / "config-reference.md"
SECURITY = REPO_ROOT / "docs" / "security.md"
GEN_CMD = "python scripts/gen_config_reference.py"

#: Every topic the epic named must be anchored in ONE of the two generated docs.
REQUIRED_TOPICS = {
    "precedence": "precedence",
    "source": "source",
    "lifecycle": "lifecycle",
    "restart/redeploy rotation": "rotation",
    "native refresh": "native refresh",
    "observability exception": "observability",
    "subprocess limitations": "subprocess",
    "behavior deltas": "behavior delta",
    "mcp/cli exposure": "exposure",
}


def _load():
    spec = importlib.util.spec_from_file_location("gen_config_reference", GEN_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gen = _load()


def _schema_keys() -> list[str]:
    """Every `section.field` config key from the typed schema."""
    from rebar._config_schema import _SECTION_CLASSES

    keys: list[str] = []
    for section, cls in _SECTION_CLASSES.items():
        for f in dataclasses.fields(cls):
            keys.append(f"{section}.{f.name}")
    return keys


# ─────────────────────────── HAPPY PATH (shown to implementer) ────────────────


def test_config_reference_lists_every_schema_key():
    """Every `section.field` config key appears, backtick-wrapped, in config-reference.md."""
    doc = gen.render_config_reference()
    for key in _schema_keys():
        assert f"`{key}`" in doc, f"config key {key!r} missing from config-reference.md"


def test_both_docs_carry_a_self_announcing_banner():
    """Each generated doc names its regenerate command within its first 8 lines."""
    for render in (gen.render_config_reference, gen.render_security):
        head = "\n".join(render().splitlines()[:8])
        assert "generated" in head.lower(), "banner must announce the doc as generated"
        assert GEN_CMD in head, f"banner must name `{GEN_CMD}`"


def test_check_mode_clean_against_committed_tree():
    """The committed docs match the generator (exit 0)."""
    assert gen.main(["--check"]) == 0


def test_regenerate_writes_both_docs():
    """A bare run writes both generated docs and exits 0."""
    assert gen.main([]) == 0
    assert CONFIG_REFERENCE.exists()
    assert SECURITY.exists()


# ─────────────────────────── EDGE CASES (HELD OUT) ────────────────────────────


# ── config.md is BYTE-untouched (its consumers must all survive) ──────────────


def test_generator_never_writes_config_md():
    """A full regenerate leaves docs/config.md byte-identical."""
    before = CONFIG_MD.read_bytes()
    assert gen.main([]) == 0
    assert CONFIG_MD.read_bytes() == before, "docs/config.md must be byte-unchanged"


def test_config_md_is_not_a_generated_target():
    """The generator's declared output paths do not include config.md."""
    both = gen.render_config_reference() + gen.render_security()
    # A generated doc names its own regenerate command; config.md must NOT be one of them.
    assert "config.md" not in {CONFIG_REFERENCE.name}  # sanity: distinct filenames
    assert CONFIG_MD.name == "config.md"
    assert "gen_config_reference.py" not in CONFIG_MD.read_text(encoding="utf-8")
    assert both  # generated content exists and is separate from config.md


# ── the cfg LIFECYCLE column (deprecations + tombstones) ──────────────────────


def test_config_reference_reflects_cfg_deprecation_aliases():
    """A permanent cfg alias rename appears with its replacement in the lifecycle column."""
    from rebar._deprecations import REGISTRY

    doc = gen.render_config_reference()
    cfg_deps = [d for d in REGISTRY.values() if d.kind == "cfg"]
    assert cfg_deps, "fixture guard: expected at least one cfg-kind deprecation"
    for dep in cfg_deps:
        assert dep.name in doc, f"deprecated cfg key {dep.name!r} missing from reference"
        if dep.replacement:
            assert dep.replacement in doc, (
                f"replacement {dep.replacement!r} for {dep.name!r} missing"
            )


def test_config_reference_reflects_cfg_tombstones():
    """Removed (tombstoned) cfg keys appear as removed in the reference."""
    from rebar._deprecations import tombstones

    doc = gen.render_config_reference()
    cfg_tombs = [t for t in tombstones() if t.kind == "cfg"]
    assert cfg_tombs, "fixture guard: expected at least one cfg-kind tombstone"
    for tomb in cfg_tombs:
        assert tomb.name in doc, f"tombstoned cfg key {tomb.name!r} missing from reference"


# ── the security secret-name inventory ────────────────────────────────────────


def test_security_doc_lists_every_adapter_secret_name():
    """Every declared adapter secret env-var NAME appears in security.md."""
    from rebar._child_env import owned_secret_names

    doc = gen.render_security()
    names = owned_secret_names()
    assert names, "fixture guard: expected declared adapter secret names"
    for name in names:
        assert name in doc, f"secret env-var {name!r} missing from security.md"


# ── REQUIRED_TOPICS coverage across BOTH docs ─────────────────────────────────


def test_required_topics_are_covered_across_both_docs():
    """Every required topic is anchored in config-reference.md or security.md."""
    combined = (gen.render_config_reference() + "\n" + gen.render_security()).lower()
    missing = [t for t, needle in REQUIRED_TOPICS.items() if needle.lower() not in combined]
    assert not missing, f"required topics not covered by either generated doc: {missing}"


# ── `--check` drift semantics: trips on drift, CRLF-safe ──────────────────────


def test_check_trips_on_a_stale_doc(tmp_path, monkeypatch):
    """A deliberately mutated committed doc makes --check return non-zero."""
    # Regenerate to a clean state first.
    assert gen.main([]) == 0
    original = CONFIG_REFERENCE.read_text(encoding="utf-8")
    try:
        CONFIG_REFERENCE.write_text(original + "\n<!-- drift -->\n", encoding="utf-8")
        assert gen.main(["--check"]) != 0
    finally:
        CONFIG_REFERENCE.write_text(original, encoding="utf-8")
    assert gen.main(["--check"]) == 0


def test_check_is_crlf_safe():
    """A CRLF-encoded checkout of a generated doc does NOT report spurious drift."""
    assert gen.main([]) == 0
    lf = CONFIG_REFERENCE.read_text(encoding="utf-8")
    try:
        crlf = lf.replace("\n", "\r\n")
        CONFIG_REFERENCE.write_bytes(crlf.encode("utf-8"))
        assert gen.main(["--check"]) == 0, "CRLF checkout must not be reported as drift"
    finally:
        CONFIG_REFERENCE.write_text(lf, encoding="utf-8")
