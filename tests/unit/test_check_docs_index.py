"""Tests for the docs-index completeness / dead-link checker (ticket f088).

The checker (scripts/check_docs_index.py) keeps docs/README.md honest: every living
top-level docs/*.md must be linked from the index (with an allowlist for intentionally
unindexed files), and every relative markdown link within docs/ must resolve.
"""

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
    """Build a fake docs/ dir; keys are filenames, values are file bodies."""
    d = tmp_path / "docs"
    d.mkdir()
    for name, body in files.items():
        (d / name).write_text(body, encoding="utf-8")
    return d


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
