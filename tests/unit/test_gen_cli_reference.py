"""Oracle for the registry-derived CLI-reference generator (RP-05 S5, ticket 6755).

S5 rebuilds ``scripts/gen_cli_reference.py`` so that EVERY command syntax/options section
in ``docs/cli-reference.md`` is DERIVED from the immutable route registry
(:mod:`rebar._cli._registry`) plus the committed package-help bytes (proven byte-current by
``scripts/gen_cli_help.py --check``). The hand-maintained intercept command census is gone:
no ``INTERCEPT_COMMANDS`` curated one-liner table and no ``ladder_intercepts()`` source-regex.
Editorial rationale is kept structurally separate and is registry-linted; parser artifacts
remain the only usage/option authority.

Assertions here are OBSERVABLE: the generator's rendered bytes, ``--check`` exit codes, the
registry-vs-doc parity, and the editorial lint's findings — never private helper names.
(The verb confirmation-line record ``MUTATION_VERBS`` is a distinct behavioral record, drift-
gated against the registry's ``_CONFIRM_SCOPE``, and is exercised by
``tests/unit/test_mutation_confirmations.py`` — out of scope here.)
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
GEN_PATH = REPO_ROOT / "scripts" / "gen_cli_reference.py"


def _load():
    spec = importlib.util.spec_from_file_location("gen_cli_reference", GEN_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gen = _load()


def _routes():
    from rebar._cli._registry import ROUTES

    return ROUTES


def _documented_routes():
    """Live (non-retired), non-hidden routes — every one must get a syntax section."""
    return [r for r in _routes() if not r.retired and not r.hidden]


# ─────────────────────────── HAPPY PATH ───────────────────────────────────────


def test_render_documents_every_live_route():
    """Every live, non-hidden route in the registry gets a backtick-headed syntax section."""
    doc = gen.render()
    for route in _documented_routes():
        assert f"### `{route.name}`" in doc, f"route {route.name!r} missing a syntax section"


def test_render_embeds_help_backed_committed_bytes():
    """A help-backed command's exact committed help bytes are embedded verbatim."""
    from rebar._cli import _help

    doc = gen.render()
    create_help = _help.subcommand_help("create")
    assert create_help is not None
    assert create_help.rstrip("\n") in doc
    assert "Usage: rebar create" in doc


def test_check_mode_clean_against_committed_tree():
    """The committed docs/cli-reference.md matches the generator (exit 0)."""
    assert gen.main(["--check"]) == 0


def _intercept_section(doc: str, name: str) -> str:
    """The fenced syntax body of route ``name`` (between its heading's ``` fences)."""
    marker = f"### `{name}`"
    assert marker in doc, f"{name} section missing"
    after = doc.split(marker, 1)[1]
    body = after.split("```", 2)
    return body[1]


def test_intercept_option_metavars_are_collapsed():
    """Version-stability: a repeated-metavar option invocation renders in the single-metavar
    form (`--output, -o {json,text}`), NOT the 3.12 repeated form — so argparse's 3.13 metavar
    collapse cannot make the committed doc drift. `review-code` has an `--output` option."""
    section = _intercept_section(gen.render(), "review-code")
    assert "--output, -o {json,text}" in section
    assert "--output {json,text}, -o {json,text}" not in section


# ─────────────────────────── EDGE CASES ───────────────────────────────────────


def test_review_plan_retry_flag_is_derived_from_canonical_help():
    """The committed review-plan artifact carries the parser-owned retry option."""
    from rebar._cli import _help
    from rebar._cli._parsers.advanced.llm import build_review_plan

    parser = build_review_plan(prog="rebar review-plan")
    help_text = parser.format_help()
    assert "--retry" in help_text
    committed = _help.subcommand_help("review-plan")
    assert committed is not None
    assert "--retry" in committed
    assert "resume only the exact latest eligible INDETERMINATE" in committed
    assert _intercept_section(gen.render(), "review-plan").strip("\n") == committed.rstrip("\n")


def test_no_curated_census_symbols_remain():
    """The intercept command-description census is deleted: no INTERCEPT_COMMANDS /
    ladder_intercepts survive as module attributes (AC2)."""
    assert not hasattr(gen, "INTERCEPT_COMMANDS")
    assert not hasattr(gen, "ladder_intercepts")


def test_generator_source_has_no_intercept_regex():
    """The generator no longer regex-parses CLI source for the intercept ladder (AC2)."""
    source = GEN_PATH.read_text(encoding="utf-8")
    assert "INTERCEPT_COMMANDS" not in source
    assert "ladder_intercepts" not in source
    assert "argv[0]" not in source


def test_retired_routes_are_not_documented():
    """Retired (unrouted) spellings get no syntax section."""
    doc = gen.render()
    for route in (r for r in _routes() if r.retired):
        assert f"### `{route.name}`" not in doc, f"retired {route.name!r} leaked into the doc"


def test_hidden_aliases_are_not_documented():
    """Hidden alias spellings (e.g. bridge-status) are not advertised as syntax sections."""
    doc = gen.render()
    hidden = [r for r in _routes() if r.hidden]
    assert hidden, "fixture guard: expected at least one hidden route"
    for route in hidden:
        assert f"### `{route.name}`" not in doc


def test_section_count_equals_documented_route_count():
    """Completeness census: exactly one syntax section per documented route — no extras,
    none missing. A route added to the registry forces a new section (derivation teeth)."""
    doc = gen.render()
    headings = {
        line[len("### ") :].strip()
        for line in doc.splitlines()
        if line.startswith("### `") and line.rstrip().endswith("`")
    }
    expected = {f"`{r.name}`" for r in _documented_routes()}
    assert headings == expected


def test_render_is_deterministic():
    """Two renders produce identical bytes (no dict-ordering / set nondeterminism)."""
    assert gen.render() == gen.render()


def test_editorial_preamble_passes_its_own_lint():
    """The shipped editorial preamble is clean under the deterministic editorial lint."""
    assert gen.lint_editorial(gen.EDITORIAL_PREAMBLE) == []


def test_editorial_lint_rejects_usage_authority():
    """An editorial block asserting its own `Usage:` grammar is rejected (parser artifacts
    are the only usage authority)."""
    bad = "Some prose.\nUsage: rebar create [options]\nMore prose.\n"
    assert gen.lint_editorial(bad), "editorial Usage: authority must be flagged"


def test_editorial_lint_rejects_option_table_authority():
    """An editorial option table (a competing option authority) is rejected."""
    bad = "Prose.\n\n| Option | Meaning |\n|--------|---------|\n| `--x` | y |\n"
    assert gen.lint_editorial(bad), "editorial option-table authority must be flagged"


def test_editorial_lint_rejects_unknown_top_level_spelling():
    """A `rebar <cmd>` reference to a non-registry spelling is rejected; a real one passes."""
    assert gen.lint_editorial("See `rebar frobnicate` for details.\n"), (
        "unknown top-level spelling must be flagged"
    )
    assert gen.lint_editorial("See `rebar create` for details.\n") == []


def test_render_raises_when_editorial_preamble_is_dirty(monkeypatch):
    """render() fails loudly rather than emit a doc with an editorial usage authority."""
    monkeypatch.setattr(gen, "EDITORIAL_PREAMBLE", "Usage: rebar create\n`rebar frobnicate`\n")
    with pytest.raises(ValueError):
        gen.render()


# ─────────────────────────── E2E via main() / drift ───────────────────────────


def test_check_mode_detects_stale_committed_doc(tmp_path: Path, monkeypatch):
    """main(--check) exits non-zero when the committed doc is stale vs the generator."""
    stale = tmp_path / "cli-reference.md"
    stale.write_text("# CLI reference\n\nstale, missing everything\n", encoding="utf-8")
    monkeypatch.setattr(gen, "DOC_PATH", stale, raising=False)
    assert gen.main(["--check"]) != 0


def test_generate_writes_full_doc_for_every_route(tmp_path: Path, monkeypatch):
    """Running the generator (no --check) writes a doc with a section for every documented
    route, and the freshly written doc is --check-clean against itself."""
    out = tmp_path / "cli-reference.md"
    monkeypatch.setattr(gen, "DOC_PATH", out, raising=False)
    assert gen.main([]) == 0
    written = out.read_text(encoding="utf-8")
    for route in _documented_routes():
        assert f"### `{route.name}`" in written
    assert gen.main(["--check"]) == 0


def test_check_mode_reflects_registry_change(monkeypatch):
    """Derivation teeth via main(): drop a route from the registry and the committed doc
    (rendered from the full registry) is detected as stale."""
    dropped = tuple(r for r in _routes() if r.name != "create")
    import rebar._cli._registry as reg

    monkeypatch.setattr(reg, "ROUTES", dropped, raising=True)
    monkeypatch.setattr(gen, "ROUTES", dropped, raising=False)
    assert gen.main(["--check"]) != 0


def test_render_omits_reference_to_reconcile_when_route_removed(monkeypatch):
    """render() is derived from the registry: removing a route removes its section."""
    dropped = tuple(r for r in _routes() if r.name != "reconcile")
    monkeypatch.setattr(gen, "ROUTES", dropped, raising=False)
    doc = gen.render()
    assert "### `reconcile`" not in doc
