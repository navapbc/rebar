"""Tests for the DCO sign-off identity consistency checker (ticket 35d2).

The checker (scripts/check_dco_identity.py) keeps contributor-facing guidance
(AGENTS.md, .agents/rules/*.md, CONTRIBUTING.md, docs/**/*.md) from reintroducing a
hardcoded personal DCO sign-off identity. Automation-owned paths (infra/,
.github/workflows/, tests/, docs/experiments/) legitimately reference a bot identity
and are excluded from the scan.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
CHK_PATH = REPO_ROOT / "scripts" / "check_dco_identity.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_dco_identity", CHK_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


chk = _load()


def _make_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, content in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return tmp_path


def test_find_violations_clean_repo_reports_nothing(tmp_path):
    repo = _make_repo(
        tmp_path,
        {
            "AGENTS.md": "Sign off with your own configured git identity.\n",
            ".agents/rules/rebar.md": "Use your own git config user.name/user.email.\n",
            "CONTRIBUTING.md": "Signed-off-by: Your Name <you@example.com>\n",
        },
    )
    assert chk.find_violations(repo) == []


def test_main_returns_zero_on_clean_repo_and_nonzero_on_violation(tmp_path):
    clean_repo = _make_repo(
        tmp_path / "clean",
        {"AGENTS.md": "Sign off with your own configured git identity.\n"},
    )
    assert chk.main(["--root", str(clean_repo)]) == 0

    dirty_repo = _make_repo(
        tmp_path / "dirty",
        {"AGENTS.md": "Signed-off-by: Joe Oakhart <joeoakhart+bot@navapbc.com>\n"},
    )
    assert chk.main(["--root", str(dirty_repo)]) != 0


def test_find_violations_flags_reintroduced_hardcoded_identity_in_scoped_file(tmp_path):
    repo = _make_repo(
        tmp_path,
        {
            "AGENTS.md": "Sign off with your own configured git identity.\n",
            ".agents/rules/rebar.md": (
                "Commit under Signed-off-by: Joe Oakhart <joeoakhart+bot@navapbc.com>\n"
            ),
        },
    )
    violations = chk.find_violations(repo)
    assert len(violations) == 1
    path, line_no, _text = violations[0]
    assert path == Path(".agents/rules/rebar.md")
    assert line_no == 1


def test_find_violations_scans_nested_docs_subdirectories(tmp_path):
    repo = _make_repo(
        tmp_path,
        {
            "docs/adr/0001-example.md": (
                "joeoakhart+bot@navapbc.com is hardcoded here by mistake.\n"
            ),
        },
    )
    violations = chk.find_violations(repo)
    assert len(violations) == 1
    assert violations[0][0] == Path("docs/adr/0001-example.md")


def test_find_violations_ignores_excluded_automation_paths(tmp_path):
    repo = _make_repo(
        tmp_path,
        {
            "infra/scripts/reviewbot-ensure-tickets.sh": (
                'EMAIL="${REVIEWBOT_GIT_USER_EMAIL:-joeoakhart+bot@navapbc.com}"\n'
            ),
            ".github/workflows/reconcile-bridge.yml": (
                "BRIDGE_BOT_EMAIL: joeoakhart+bot@navapbc.com\n"
            ),
            "tests/fixtures/review_bot_merge/mergelist_clean.json": ('{"name": "Joe Oakhart"}\n'),
            "docs/experiments/old-run.md": (
                "Signed-off-by: Joe Oakhart <joeoakhart+bot@navapbc.com>\n"
            ),
        },
    )
    assert chk.find_violations(repo) == []


def test_find_violations_ignores_files_outside_scoped_globs(tmp_path):
    repo = _make_repo(
        tmp_path,
        {
            "src/rebar/some_module.py": (
                "# joeoakhart+bot@navapbc.com  (unrelated code comment)\n"
            ),
        },
    )
    assert chk.find_violations(repo) == []
