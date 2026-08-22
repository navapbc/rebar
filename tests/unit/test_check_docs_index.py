"""Tests for documentation index membership and repository-relative links."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
CHK_PATH = REPO_ROOT / "scripts" / "check_docs_index.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_docs_index", CHK_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


chk = _load()


def _make_docs(tmp_path: Path, files: dict[str, str]) -> Path:
    """Build a synthetic docs directory from relative names and file bodies."""
    d = tmp_path / "docs"
    d.mkdir()
    for name, body in files.items():
        (d / name).write_text(body, encoding="utf-8")
    return d


def _write_markdown_tree(root: Path, files: dict[str, str]) -> None:
    """Write Markdown fixtures below ``root``."""
    for name, body in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")


# ─────────────────────────── HAPPY PATH (shown to implementer) ────────────────


def test_current_repo_index_is_clean():
    """The real, committed docs/README.md passes both checks (exit 0)."""
    assert chk.main(["--check"]) == 0


def test_clean_synthetic_tree_has_no_findings(tmp_path: Path):
    """A well-formed docs tree — every living doc linked, every link resolving —
    yields no unindexed docs and no broken links."""
    docs = _make_docs(
        tmp_path,
        {
            "README.md": "# Index\n\n- [alpha.md](alpha.md)\n- [beta.md](beta.md)\n",
            "alpha.md": "# Alpha\n\nSee [beta.md](beta.md).\n",
            "beta.md": "# Beta\n",
        },
    )
    assert chk.find_unindexed(docs) == []
    assert chk.find_broken_links(docs) == []


# ─────────────────────────── EDGE CASES (HELD OUT) ────────────────────────────


def test_unindexed_living_doc_is_flagged(tmp_path: Path):
    """A living top-level docs/*.md not linked from the index is reported."""
    docs = _make_docs(
        tmp_path,
        {
            "README.md": "# Index\n\n- [alpha.md](alpha.md)\n",
            "alpha.md": "# Alpha\n",
            "orphan.md": "# Orphan — not linked anywhere\n",
        },
    )
    unindexed = chk.find_unindexed(docs)
    assert "orphan.md" in unindexed
    assert "alpha.md" not in unindexed


def test_index_itself_not_flagged(tmp_path: Path):
    """README.md (the index) is never required to link to itself."""
    docs = _make_docs(
        tmp_path,
        {"README.md": "# Index\n\n- [alpha.md](alpha.md)\n", "alpha.md": "# Alpha\n"},
    )
    assert "README.md" not in chk.find_unindexed(docs)


def test_allowlist_suppresses_local_md(tmp_path: Path):
    """A *.local.md file (git-ignored) is not required to be indexed."""
    docs = _make_docs(
        tmp_path,
        {
            "README.md": "# Index\n\n- [alpha.md](alpha.md)\n",
            "alpha.md": "# Alpha\n",
            "notes.local.md": "# Private local notes\n",
        },
    )
    assert "notes.local.md" not in chk.find_unindexed(docs)


def test_prose_mention_is_not_a_link(tmp_path: Path):
    """A doc merely NAMED in prose (not inside a markdown link target) still counts
    as unindexed — only a real ](target) link satisfies the index requirement."""
    docs = _make_docs(
        tmp_path,
        {
            # ghost.md appears as bare text, not as ](ghost.md)
            "README.md": "# Index\n\n- [alpha.md](alpha.md)\n\nDo not move ghost.md.\n",
            "alpha.md": "# Alpha\n",
            "ghost.md": "# Ghost\n",
        },
    )
    assert "ghost.md" in chk.find_unindexed(docs)


def test_broken_relative_link_is_flagged(tmp_path: Path):
    """A relative markdown link within docs/ to a nonexistent docs/ target is reported."""
    docs = _make_docs(
        tmp_path,
        {
            "README.md": "# Index\n\n- [alpha.md](alpha.md)\n",
            "alpha.md": "# Alpha\n\nSee [gone](./nonexistent.md).\n",
        },
    )
    broken = chk.find_broken_links(docs)
    targets = {t for _src, t in broken}
    assert any("nonexistent.md" in t for t in targets)


def test_valid_relative_link_with_anchor_is_ok(tmp_path: Path):
    """A link with a #fragment to an existing file is NOT broken (fragment stripped)."""
    docs = _make_docs(
        tmp_path,
        {
            "README.md": "# Index\n\n- [alpha.md](alpha.md)\n",
            "alpha.md": "# Alpha\n\nSee [beta section](beta.md#intro).\n",
            "beta.md": "# Beta\n\n## intro\n",
        },
    )
    broken = chk.find_broken_links(docs)
    assert all("beta.md" not in t for _s, t in broken)


def test_external_links_are_ignored(tmp_path: Path):
    """http(s):// and mailto: links are not treated as broken relative links."""
    docs = _make_docs(
        tmp_path,
        {
            "README.md": "# Index\n\n- [alpha.md](alpha.md)\n",
            "alpha.md": "# Alpha\n\n[site](https://example.com) [mail](mailto:x@y.z)\n",
        },
    )
    assert chk.find_broken_links(docs) == []


def test_declared_markdown_source_boundary_is_exact(tmp_path: Path):
    included = {
        "README.md",
        ".agents/rules.md",
        ".github/pull_request_template.md",
        "docs/guide.md",
        "examples/agent-skills/sample/SKILL.md",
        "infra/runbooks/operations.md",
        "infra/service/README.md",
        "scripts/tool/README.md",
        "src/rebar/_guides/guide.md",
        "src/rebar/llm/eval_specs/README.plan-review.md",
        "templates/AGENTS.md",
        "tests/external/system/README.md",
        "tests/unit/fixtures/README.md",
    }
    excluded = {
        ".hidden.md",
        "notes.local.md",
        "docs/notes.local.md",
        ".joe-janitor/report.md",
        ".rebar/prompts/prompt.md",
        "src/rebar/llm/reviewers/reviewer.md",
        "tests/fixtures/corpus/README.md",
        "tests/scripts/fixtures/copy.md",
        "tests/unit/rebar_reconciler/integration_gates/story.md",
        "infra/service/guide.md",
        "scripts/tool/guide.md",
        "tests/external/system/guide.md",
    }
    _write_markdown_tree(tmp_path, {path: "# Document\n" for path in included | excluded})

    sources = {
        path.relative_to(tmp_path).as_posix() for path in chk.find_markdown_sources(tmp_path)
    }

    assert sources == included


def test_excluded_source_can_be_a_valid_link_target(tmp_path: Path):
    _write_markdown_tree(
        tmp_path,
        {
            "docs/source.md": "[prompt](../.rebar/prompts/prompt.md)\n",
            ".rebar/prompts/prompt.md": "# Prompt\n",
        },
    )

    assert chk.find_link_findings(tmp_path) == []


def test_missing_and_escaping_targets_have_structured_findings(tmp_path: Path):
    _write_markdown_tree(
        tmp_path,
        {
            "docs/source.md": (
                "# Source\n[missing](missing.md?download=1#part)\n![outside](../../outside.png)\n"
            )
        },
    )

    findings = chk.find_link_findings(tmp_path)

    assert findings == [
        chk.LinkFinding(
            source_path="docs/source.md",
            line_number=2,
            raw_target="missing.md?download=1#part",
            normalized_target_path="docs/missing.md",
            reason="missing-target",
        ),
        chk.LinkFinding(
            source_path="docs/source.md",
            line_number=3,
            raw_target="../../outside.png",
            normalized_target_path="../outside.png",
            reason="outside-repository",
        ),
    ]


def test_ignored_syntax_and_valid_qualified_target_have_no_findings(tmp_path: Path):
    _write_markdown_tree(
        tmp_path,
        {
            "docs/source.md": (
                "[fragment](#section)\n"
                "[site](https://example.com/missing.md)\n"
                "[mail](mailto:docs@example.com)\n"
                "`[inline](missing-inline.md)`\n"
                "```md\n"
                "[fenced](missing-fenced.md)\n"
                "```\n"
                "[guide](guide.md?download=1#section)\n"
            ),
            "docs/guide.md": "# Guide\n",
        },
    )

    assert chk.find_link_findings(tmp_path) == []


def test_multiline_link_and_linked_image_are_scanned(tmp_path: Path):
    _write_markdown_tree(
        tmp_path,
        {
            "README.md": (
                "[guide](\n"
                "  missing-guide.md\n"
                ")\n"
                "[![badge](docs/badge.svg)](\n"
                "  missing-license\n"
                ")\n"
            ),
            "docs/badge.svg": "<svg/>\n",
        },
    )

    assert [
        (finding.line_number, finding.raw_target) for finding in chk.find_link_findings(tmp_path)
    ] == [(1, "missing-guide.md"), (4, "missing-license")]


def test_balanced_angle_and_escaped_destinations_resolve(tmp_path: Path):
    _write_markdown_tree(
        tmp_path,
        {
            "docs/source.md": (
                '[balanced](guide(v1).md "Guide")\n'
                '[angle](<guide with spaces.md> "Guide")\n'
                "[escaped](guide\\(v1\\).md)\n"
            ),
            "docs/guide(v1).md": "# Guide\n",
            "docs/guide with spaces.md": "# Guide\n",
        },
    )

    assert chk.find_link_findings(tmp_path) == []


# ─────────────────────────── E2E via main() (HELD OUT) ────────────────────────


def test_main_check_exit_nonzero_on_unindexed(tmp_path: Path, monkeypatch):
    """main(--check) exits non-zero when a synthetic tree has an unindexed doc."""
    docs = _make_docs(
        tmp_path,
        {"README.md": "# Index\n", "orphan.md": "# Orphan\n"},
    )
    monkeypatch.setattr(chk, "DEFAULT_DOCS_DIR", docs, raising=False)
    assert chk.main(["--check"]) != 0


def test_main_check_exit_nonzero_on_broken_link(tmp_path: Path, monkeypatch):
    """main(--check) exits non-zero when a synthetic tree has a broken relative link."""
    docs = _make_docs(
        tmp_path,
        {
            "README.md": "# Index\n\n- [alpha.md](alpha.md)\n",
            "alpha.md": "# Alpha\n\n[x](./missing.md)\n",
        },
    )
    monkeypatch.setattr(chk, "DEFAULT_DOCS_DIR", docs, raising=False)
    assert chk.main(["--check"]) != 0


def test_main_clean_synthetic_tree_exit_zero(tmp_path: Path, monkeypatch):
    """main(--check) exits 0 on a well-formed synthetic tree."""
    docs = _make_docs(
        tmp_path,
        {"README.md": "# Index\n\n- [alpha.md](alpha.md)\n", "alpha.md": "# Alpha\n"},
    )
    monkeypatch.setattr(chk, "DEFAULT_DOCS_DIR", docs, raising=False)
    assert chk.main(["--check"]) == 0


def test_main_reports_every_structured_link_field(tmp_path: Path, monkeypatch, capsys):
    docs = _make_docs(
        tmp_path,
        {
            "README.md": "# Index\n\n- [alpha.md](alpha.md)\n",
            "alpha.md": "[missing](missing.md?download=1#part)\n",
        },
    )
    monkeypatch.setattr(chk, "DEFAULT_DOCS_DIR", docs, raising=False)

    assert chk.main(["--check"]) == 1
    error = capsys.readouterr().err
    assert "docs/alpha.md" in error
    assert "line 1" in error
    assert "missing.md?download=1#part" in error
    assert "docs/missing.md" in error
    assert "missing-target" in error


def test_all_docs_relative_links_resolve():
    """Every declared Markdown source has resolvable repository-relative targets."""
    assert chk.find_link_findings(REPO_ROOT) == []
