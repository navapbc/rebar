"""WS1 (epic b744): the diff context-assembler.

Pins: the assembled `context` string shape (changed-files / orientation / diff sections),
changed-file parsing from a unified diff, the truncation path, and the IMPORT ISOLATION
contract — `assemble.py` must not depend on the single-pass `code_review` route so WS4's
retirement of it cannot break the assembler.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from rebar.llm.code_review import assemble as A

pytestmark = pytest.mark.unit

_SAMPLE_DIFF = """\
diff --git a/src/rebar/foo.py b/src/rebar/foo.py
index 1111111..2222222 100644
--- a/src/rebar/foo.py
+++ b/src/rebar/foo.py
@@ -1,3 +1,4 @@
 def foo():
-    return 1
+    return 2
+    # changed
diff --git a/docs/bar.md b/docs/bar.md
index 3333333..4444444 100644
--- a/docs/bar.md
+++ b/docs/bar.md
@@ -1 +1 @@
-old
+new
"""


def test_changed_from_diff_parses_new_paths_dedup_ordered():
    files = A.changed_from_diff(_SAMPLE_DIFF)
    assert files == ["src/rebar/foo.py", "docs/bar.md"]


def test_changed_from_diff_skips_dev_null_deletions():
    deletion = (
        "diff --git a/gone.py b/gone.py\ndeleted file mode 100644\n--- a/gone.py\n+++ /dev/null\n"
    )
    # The `diff --git ... b/gone.py` header still names the path; /dev/null is skipped.
    assert A.changed_from_diff(deletion) == ["gone.py"]


def test_assemble_from_diff_text_builds_context_sections():
    ctx = A.assemble_diff_context(diff_text=_SAMPLE_DIFF)
    assert isinstance(ctx, A.DiffContext)
    assert ctx.changed_files == ["src/rebar/foo.py", "docs/bar.md"]
    s = ctx.context
    assert "## Changed files (2)" in s
    assert "## Orientation" in s
    assert "## Diff" in s
    assert "```diff" in s
    # the diff body is present verbatim (not truncated for a small diff)
    assert "return 2" in s
    assert "(diff truncated" not in s


def test_assemble_respects_explicit_changed_files():
    ctx = A.assemble_diff_context(diff_text="(opaque)", changed_files=["a/b.py"])
    assert ctx.changed_files == ["a/b.py"]
    assert "## Changed files (1)" in ctx.context


def test_context_truncates_oversized_diff_with_notice():
    big = "diff --git a/x.py b/x.py\n+++ b/x.py\n" + ("+x\n" * 100000)
    ctx = A.assemble_diff_context(diff_text=big, diff_char_cap=500)
    s = ctx.context
    assert "(diff truncated; use your file tools for the rest)" in s
    # the fenced diff payload is bounded by the cap (+ notice), not the full 300k chars
    assert len(s) < 5000


def test_empty_changed_files_renders_none_placeholder():
    ctx = A.assemble_diff_context(diff_text="", changed_files=[])
    assert "## Changed files (0)" in ctx.context
    assert "(none)" in ctx.context


def test_assemble_does_not_import_single_pass_route():
    """AC: assemble.py is self-contained — it must not import the single-pass code_review
    route (review_code / select_code_reviewers / _review_code_inner), so WS4's retirement of
    that route leaves the assembler standing. We AST-inspect the imports."""
    src = pathlib.Path("src/rebar/llm/code_review/assemble.py").read_text()
    tree = ast.parse(src)
    forbidden = {"review_code", "select_code_reviewers", "_review_code_inner", "_compose_context"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            # No `from rebar.llm.code_review import <single-pass symbol>` and no bare
            # `from . import <single-pass symbol>` pulling the package __init__'s API.
            module = node.module or ""
            if module in ("rebar.llm.code_review", "") or module.endswith("code_review"):
                names = {a.name for a in node.names}
                assert not (names & forbidden), (
                    f"assemble.py must not import single-pass symbols {names & forbidden} "
                    f"from {module!r}"
                )
        if isinstance(node, ast.Import):
            for a in node.names:
                assert a.name != "rebar.llm.code_review.__init__"
