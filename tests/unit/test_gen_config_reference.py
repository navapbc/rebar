"""Oracle for the config-reference generator (ticket 3199, RP-04 S7.4).

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

The happy-path subset (schema-key coverage, banners, a clean --check against the
committed tree, and a bare regenerate) was the only part shown to the implementer during
development; the edge/E2E oracle (config.md preservation, the lifecycle column, secret
inventory, topic coverage, CRLF newline-safety, drift-trips) was withheld and is
consolidated below. Tests that need to WRITE generated output redirect the generator's
output paths to a tmp dir via the ``redirect_outputs`` fixture, so no test mutates the
committed working tree; the one exception is
``test_check_mode_clean_against_committed_tree``, which is read-only and asserts the
committed docs are current.
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
    "cli exposure": "Exposure (CLI exposure)",
    "scanner-lock authority": "review scanner lock authority",
    "checksum provenance": "checksum provenance",
    "withdrawn-asset recovery": "withdrawn",
}


def _load():
    spec = importlib.util.spec_from_file_location("gen_config_reference", GEN_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gen = _load()


@pytest.fixture
def redirect_outputs(tmp_path, monkeypatch):
    """Point the generator's output paths at a tmp dir so no committed doc is mutated.

    Returns (config_reference_path, security_path) under tmp_path.
    """
    cfg = tmp_path / "config-reference.md"
    sec = tmp_path / "security.md"
    monkeypatch.setattr(gen, "CONFIG_REFERENCE_PATH", cfg)
    monkeypatch.setattr(gen, "SECURITY_PATH", sec)
    return cfg, sec


def _schema_keys() -> list[str]:
    """Every `section.field` config key from the typed schema."""
    from rebar._config_schema import _SECTION_CLASSES

    keys: list[str] = []
    for section, cls in _SECTION_CLASSES.items():
        for f in dataclasses.fields(cls):
            keys.append(f"{section}.{f.name}")
    return keys


def _schema_fields() -> list[tuple[str, dataclasses.Field]]:
    """Return each public key with its typed dataclass field."""
    from rebar._config_schema import _SECTION_CLASSES

    return [
        (f"{section}.{item.name}", item)
        for section, cls in _SECTION_CLASSES.items()
        for item in dataclasses.fields(cls)
    ]


def _row_for(doc: str, name: str) -> str:
    """The markdown table row whose FIRST column is ``name`` (backtick-wrapped), or ''.

    Anchoring on the first column avoids matching rows that merely mention ``name`` in a
    later cell (e.g. a replacement's row noting it 'replaces removed key `name`').
    """
    needle = f"`{name}`"
    for line in doc.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(f"| {needle} ") and needle in stripped:
            return line
    return ""


# ─────────────────────────── HAPPY PATH (shown to implementer) ────────────────


def test_config_reference_lists_every_schema_key():
    """Every `section.field` config key appears, backtick-wrapped, in config-reference.md."""
    doc = gen.render_config_reference()
    for key in _schema_keys():
        assert f"`{key}`" in doc, f"config key {key!r} missing from config-reference.md"


def test_every_schema_field_has_public_description():
    """Every generated setting carries client-facing description metadata."""
    missing = [
        key
        for key, item in _schema_fields()
        if not isinstance(item.metadata.get("public_description"), str)
        or not item.metadata["public_description"].strip()
    ]
    assert not missing, f"configuration fields lack public descriptions: {missing}"


@pytest.mark.parametrize(
    "metadata",
    [{}, {"public_description": ""}, {"public_description": "   "}],
)
def test_schema_rows_reject_missing_or_empty_description(metadata, monkeypatch):
    """Generation names the key whose public description is absent or empty."""
    from rebar import _config_schema

    incomplete = dataclasses.make_dataclass(
        "IncompleteConfig",
        [("value", str, dataclasses.field(default="", metadata=metadata))],
    )
    monkeypatch.setattr(_config_schema, "_SECTION_CLASSES", {"incomplete": incomplete})

    with pytest.raises(ValueError, match=r"incomplete\.value"):
        gen._schema_rows()


def test_config_reference_renders_and_escapes_public_descriptions(monkeypatch):
    """The Description column keeps table-sensitive prose in one cell."""
    from rebar import _config_schema

    described = dataclasses.make_dataclass(
        "DescribedConfig",
        [
            (
                "choice",
                str,
                dataclasses.field(
                    default="",
                    metadata={
                        "public_description": (
                            "Selects `left | right` and records the chosen branch.\n"
                            "Continues in the same table cell."
                        )
                    },
                ),
            )
        ],
    )
    monkeypatch.setattr(_config_schema, "_SECTION_CLASSES", {"sample": described})

    rendered = gen.render_config_reference()
    row = _row_for(rendered, "sample.choice")
    assert "| Key | Type | Default | Description | Lifecycle |" in rendered
    assert r"left \| right" in row
    assert "Continues in the same table cell." in row


def test_both_docs_carry_a_self_announcing_banner():
    """Each generated doc names its regenerate command within its first 8 lines."""
    for render in (gen.render_config_reference, gen.render_security):
        head = "\n".join(render().splitlines()[:8])
        assert "generated" in head.lower(), "banner must announce the doc as generated"
        assert GEN_CMD in head, f"banner must name `{GEN_CMD}`"


def test_check_mode_clean_against_committed_tree():
    """The committed docs match the generator (exit 0). Read-only — no mutation."""
    assert gen.main(["--check"]) == 0


def test_regenerate_writes_both_docs(redirect_outputs):
    """A bare run writes both generated docs and exits 0."""
    cfg, sec = redirect_outputs
    assert gen.main([]) == 0
    assert cfg.exists()
    assert sec.exists()


# ─────────────────────────── EDGE CASES (HELD OUT) ────────────────────────────


# ── config.md is BYTE-untouched (its consumers must all survive) ──────────────


def test_generator_never_writes_config_md(redirect_outputs):
    """A full regenerate leaves docs/config.md byte-identical and never targets it."""
    before = CONFIG_MD.read_bytes()
    assert CONFIG_MD not in {gen.CONFIG_REFERENCE_PATH, gen.SECURITY_PATH}
    assert gen.main([]) == 0
    assert CONFIG_MD.read_bytes() == before, "docs/config.md must be byte-unchanged"


def test_config_md_is_not_a_generated_target():
    """config.md is distinct from the generated docs and carries no generator banner."""
    both = gen.render_config_reference() + gen.render_security()
    assert CONFIG_MD.name == "config.md"
    assert "gen_config_reference.py" not in CONFIG_MD.read_text(encoding="utf-8")
    assert both  # generated content exists and is separate from config.md


# ── the cfg LIFECYCLE column (deprecations + tombstones) ──────────────────────


def test_config_reference_reflects_cfg_deprecation_aliases():
    """Each cfg alias appears in its own row WITH its replacement and status string."""
    from rebar._deprecations import REGISTRY

    doc = gen.render_config_reference()
    cfg_deps = [d for d in REGISTRY.values() if d.kind == "cfg"]
    assert cfg_deps, "fixture guard: expected at least one cfg-kind deprecation"
    for dep in cfg_deps:
        row = _row_for(doc, dep.name)
        assert row, f"deprecated cfg key {dep.name!r} missing from a reference table row"
        status = "permanent alias" if dep.permanent else f"deprecated (removal in {dep.remove_in})"
        assert status in row, f"lifecycle status {status!r} for {dep.name!r} not in its row: {row}"
        if dep.replacement:
            assert f"`{dep.replacement}`" in row, (
                f"replacement {dep.replacement!r} for {dep.name!r} not in its row: {row}"
            )


def test_config_reference_reflects_cfg_tombstones():
    """Each removed cfg key appears in its own row WITH its removal behavior."""
    from rebar._deprecations import tombstones

    doc = gen.render_config_reference()
    cfg_tombs = [t for t in tombstones() if t.kind == "cfg"]
    assert cfg_tombs, "fixture guard: expected at least one cfg-kind tombstone"
    for tomb in cfg_tombs:
        row = _row_for(doc, tomb.name)
        assert row, f"tombstoned cfg key {tomb.name!r} missing from a reference table row"
        assert tomb.behavior in row, (
            f"removal behavior {tomb.behavior!r} for {tomb.name!r} not in its row: {row}"
        )


# ── the security secret-name inventory ────────────────────────────────────────


def test_security_doc_lists_every_adapter_secret_name():
    """Every declared adapter secret env-var NAME appears in security.md."""
    from rebar._child_env import owned_secret_names

    doc = gen.render_security()
    names = owned_secret_names()
    assert names, "fixture guard: expected declared adapter secret names"
    for name in names:
        assert name in doc, f"secret env-var {name!r} missing from security.md"


def test_security_doc_documents_scanner_lock_update_contract():
    """The generated security narrative owns the scanner-lock operating procedure."""
    doc = gen.render_security()
    assert "## Review scanner lock authority" in doc
    assert "infra/compose/review-scanners.lock.json" in doc
    assert "gitleaks_<version>_checksums.txt" in doc
    assert "linux/amd64" in doc and "linux/arm64" in doc
    assert "uv pip compile" in doc and "--require-hashes" in doc
    assert "withdrawn" in doc and "do not loosen digest checking" in doc


# ── REQUIRED_TOPICS coverage across BOTH docs ─────────────────────────────────


def test_required_topics_are_covered_across_both_docs():
    """Every required topic is anchored in config-reference.md or security.md."""
    combined = (gen.render_config_reference() + "\n" + gen.render_security()).lower()
    missing = [t for t, needle in REQUIRED_TOPICS.items() if needle.lower() not in combined]
    assert not missing, f"required topics not covered by either generated doc: {missing}"


def test_config_exposure_guidance_names_only_the_cli_surface():
    """Configuration exposure guidance describes the implemented CLI command."""
    rendered = gen.render_config_reference()
    exposure = next(line for line in rendered.splitlines() if line.startswith("- **Exposure ("))

    assert (
        "## Rotation, native refresh, and exposure" in rendered,
        exposure,
    ) == (
        True,
        "- **Exposure (CLI exposure).** The read-only `rebar config` command displays "
        "resolved typed configuration values and the precedence source for each key. "
        "Adapter credentials are not typed configuration keys and do not appear in this "
        "output. See `docs/security.md` for the adapter credential names that rebar removes "
        "from unrelated child environments.",
    )


@pytest.mark.parametrize(
    "unsupported_claim",
    [
        "MCP/CLI exposure",
        "MCP config-transparency surface",
        "MCP configuration surface",
    ],
)
def test_config_exposure_guidance_rejects_unsupported_mcp_claims(unsupported_claim):
    """Rendered configuration guidance names no MCP configuration surface."""
    assert unsupported_claim not in gen.render_config_reference()


# ── `--check` drift semantics: trips on drift, CRLF-safe ──────────────────────


def test_check_trips_on_a_stale_doc(redirect_outputs):
    """A deliberately mutated generated doc makes --check return non-zero."""
    cfg, _sec = redirect_outputs
    assert gen.main([]) == 0  # seed clean tmp copies
    cfg.write_text(cfg.read_text(encoding="utf-8") + "\n<!-- drift -->\n", encoding="utf-8")
    assert gen.main(["--check"]) != 0


def test_check_is_crlf_safe(redirect_outputs):
    """A CRLF-encoded checkout of a generated doc does NOT report spurious drift."""
    cfg, _sec = redirect_outputs
    assert gen.main([]) == 0  # seed clean tmp copies
    crlf = cfg.read_text(encoding="utf-8").replace("\n", "\r\n")
    cfg.write_bytes(crlf.encode("utf-8"))
    assert gen.main(["--check"]) == 0, "CRLF checkout must not be reported as drift"


# ── multi-alias canonical-key collisions (ticket b0a5-f5c0) ───────────────────


def _collision_key() -> str:
    """A real schema key to stand in as the shared canonical replacement target."""
    keys = sorted(_schema_keys())
    assert keys, "fixture guard: the typed schema must expose at least one key"
    return keys[0]


def test_two_aliases_on_one_canonical_key_both_render(monkeypatch):
    """Two deprecated aliases replaced by the SAME key both keep their lifecycle note."""
    key = _collision_key()
    monkeypatch.setattr(
        gen,
        "_cfg_deprecations",
        lambda: [
            ("legacy.first_alias", key, "permanent alias"),
            ("legacy.second_alias", key, "deprecated (removal in 9.9.9)"),
        ],
    )
    monkeypatch.setattr(gen, "_cfg_tombstones", list)
    row = _row_for(gen.render_config_reference(), key)
    assert row, f"schema key {key!r} missing from a reference table row"
    assert "legacy.first_alias" in row, f"first alias note dropped from {key!r} row: {row}"
    assert "legacy.second_alias" in row, f"second alias note dropped from {key!r} row: {row}"


def test_alias_and_tombstone_on_one_canonical_key_both_render(monkeypatch):
    """A deprecated alias and a tombstone sharing one replacement both keep their note."""
    key = _collision_key()
    monkeypatch.setattr(
        gen, "_cfg_deprecations", lambda: [("legacy.alias", key, "permanent alias")]
    )
    monkeypatch.setattr(gen, "_cfg_tombstones", lambda: [("legacy.removed", key, "warns")])
    row = _row_for(gen.render_config_reference(), key)
    assert row, f"schema key {key!r} missing from a reference table row"
    assert "legacy.alias" in row, f"alias note dropped from {key!r} row: {row}"
    assert "legacy.removed" in row, f"tombstone note dropped from {key!r} row: {row}"
