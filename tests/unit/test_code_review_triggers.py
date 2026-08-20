"""S1 (054b decorous-ethnic-xiaosaurus): project-extensible triggers for BUILT-IN code-review
overlays.

A project ``.rebar/criteria_routing.json`` ``code_review`` entry keyed by a BUILT-IN overlay id
(a member of ``registry.OVERLAY_IDS``) may carry two ADDITIVE keys:

* ``trigger_tokens`` — literal substrings matched case-sensitively against the added (``+``) and
  removed (``-``) lines of the unified diff (the ``+++``/``---`` file headers excluded), extending
  ``registry.content_triggered_overlays``;
* ``applies_to`` — file globs UNIONED with the overlay's committed globs, extending
  ``registry.glob_triggered_overlays``.

Both are consumed with ``repo_root`` threaded from the single call site
``code_review.workflow_ops.overlay_union`` (which reads ``ctx.repo_root``). Oracle: the overlay-id
lists the trigger functions return, and the ``include_<overlay>`` flags the ``overlay_union`` step
emits.

This module is the shared home for the concurrency-overlay epic's trigger tests (S1/S2/S5).
"""

from __future__ import annotations

import json

import pytest

from rebar.llm.code_review import registry as reg
from rebar.llm.code_review import workflow_ops as _cr_ops  # noqa: F401 — registers overlay_union
from rebar.llm.workflow import executor as _ex

pytestmark = pytest.mark.unit


def _make_repo(tmp_path, code_review: dict) -> str:
    """Write a project overlay carrying only a ``code_review`` map; return the repo root."""
    d = tmp_path / ".rebar"
    d.mkdir(parents=True, exist_ok=True)
    (d / "criteria_routing.json").write_text(
        json.dumps({"code_review": code_review}), encoding="utf-8"
    )
    return str(tmp_path)


def _run_op(name, inputs, repo_root=None):
    ctx = _ex.StepContext(
        run_id="r",
        step_id="s",
        kind="uses",
        step={"uses": name},
        inputs=inputs,
        workflow={},
        repo_root=repo_root,
    )
    return _ex.STEP_REGISTRY[name](ctx)


# ── HAPPY PATH ──────────────────────────────────────────────────────────────────────────────
def test_project_trigger_tokens_fire_builtin_on_added_line(tmp_path):
    """A project ``trigger_tokens`` list on a BUILT-IN overlay id (`performance`) causes
    ``content_triggered_overlays`` to return that id for a diff whose ADDED line contains the
    literal token — the core project-extension mechanism."""
    root = _make_repo(tmp_path, {"performance": {"trigger_tokens": ["FROBNICATE("]}})
    diff = "--- a/x.py\n+++ b/x.py\n@@ -0,0 +1 @@\n+    result = FROBNICATE(payload)\n"
    assert "performance" in reg.content_triggered_overlays(diff, root)


# ── EDGE: removed-line firing (deleting synchronization is concurrency-introducing) ──────────
def test_project_trigger_tokens_fire_on_removed_line(tmp_path):
    root = _make_repo(tmp_path, {"performance": {"trigger_tokens": ["HOTPATH("]}})
    diff = "--- a/x.py\n+++ b/x.py\n@@ -1 +0,0 @@\n-    result = HOTPATH(payload)\n"
    assert "performance" in reg.content_triggered_overlays(diff, root)


# ── EDGE: applies_to globs UNION (not replace) with the committed globs ──────────────────────
def test_project_applies_to_globs_union_with_committed(tmp_path):
    # `security` ships committed globs (**/auth* ...). A project glob is ADDED, not substituted:
    # BOTH a committed-glob match (auth.py) and the project-glob match (weird.frobext) must fire.
    root = _make_repo(tmp_path, {"security": {"applies_to": ["**/*.frobext"]}})
    committed = reg.glob_triggered_overlays(["auth.py"], root)
    project = reg.glob_triggered_overlays(["weird.frobext"], root)
    assert "security" in committed, "committed glob must still fire (union, not replace)"
    assert "security" in project, "project glob must also fire"


# ── NEGATIVE CONTROL: no project overlay ⇒ behavior byte-identical to today ──────────────────
def test_no_overlay_content_trigger_unchanged(tmp_path):
    root = _make_repo(tmp_path, {})  # empty code_review map = no extensions
    # A bare add of a token-shaped line does NOT fire any overlay when no project declares it.
    diff = "--- a/x.py\n+++ b/x.py\n@@ -0,0 +1 @@\n+    result = FROBNICATE(payload)\n"
    assert reg.content_triggered_overlays(diff, root) == []
    # And the committed deletion-impact behavior is preserved under the new signature.
    di = "--- a/x.py\n+++ b/x.py\n@@ -1,2 +0,0 @@\n-def helper(x):\n-    return x\n"
    assert reg.content_triggered_overlays(di, root) == ["deletion-impact"]
    assert reg.content_triggered_overlays(di) == ["deletion-impact"]  # no repo_root at all


def test_no_overlay_glob_trigger_unchanged(tmp_path):
    root = _make_repo(tmp_path, {})
    # security's committed globs still fire; an unmatched exotic file fires nothing.
    assert "security" in reg.glob_triggered_overlays(["auth.py"], root)
    assert reg.glob_triggered_overlays(["weird.frobext"], root) == []
    assert reg.glob_triggered_overlays(["weird.frobext"]) == []  # no repo_root at all


# ── MALFORMED project entries are IGNORED at consumption, never raised ───────────────────────
def test_malformed_trigger_tokens_string_ignored(tmp_path):
    # A STRING (not a list) must be ignored wholesale — NOT iterated into single-char tokens.
    root = _make_repo(tmp_path, {"performance": {"trigger_tokens": "FROBNICATE("}})
    diff = "--- a/x.py\n+++ b/x.py\n@@ -0,0 +1 @@\n+    result = FROBNICATE(payload)\n"
    assert "performance" not in reg.content_triggered_overlays(diff, root)


def test_malformed_non_string_tokens_skipped_valid_ones_kept(tmp_path):
    root = _make_repo(tmp_path, {"performance": {"trigger_tokens": ["OKTOKEN(", 123, None]}})
    diff = "--- a/x.py\n+++ b/x.py\n@@ -0,0 +1 @@\n+    result = OKTOKEN(payload)\n"
    assert "performance" in reg.content_triggered_overlays(diff, root)


def test_unknown_overlay_id_entry_ignored(tmp_path):
    root = _make_repo(tmp_path, {"totally-not-an-overlay": {"trigger_tokens": ["ZZZMARKER"]}})
    diff = "--- a/x.py\n+++ b/x.py\n@@ -0,0 +1 @@\n+    x = ZZZMARKER\n"
    assert reg.content_triggered_overlays(diff, root) == []


# ── E2E: repo_root threads through the REAL overlay_union step (ctx.repo_root) ────────────────
def test_overlay_union_step_threads_repo_root(tmp_path):
    root = _make_repo(tmp_path, {"performance": {"trigger_tokens": ["FROBNICATE("]}})
    diff = "--- a/x.py\n+++ b/x.py\n@@ -0,0 +1 @@\n+    result = FROBNICATE(payload)\n"
    out = _run_op("overlay_union", {"changed_files": ["x.py"], "diff_text": diff}, repo_root=root)
    assert out["include_performance"] is True
    assert "performance" in out["to_run"]
    # And WITHOUT repo_root the same diff does not fire performance (proves the threading matters).
    out_no_root = _run_op("overlay_union", {"changed_files": ["x.py"], "diff_text": diff})
    assert out_no_root["include_performance"] is False
