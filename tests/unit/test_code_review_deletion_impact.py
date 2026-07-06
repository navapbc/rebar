"""Story f983 (epic b744): the content-triggered overlay seam + the ``deletion-impact`` overlay.

Pins, OFFLINE (no tokens):
  - ``registry.content_triggered_overlays`` fires ``["deletion-impact"]`` on a diff whose
    REMOVED (`-`) lines drop a def/class/function-signature, and is silent for an add-only
    diff, a body-only edit that keeps the signature, or removed comment/blank lines only.
  - ``deletion-impact`` is a member of the closed ``OVERLAY_IDS`` enum AND has a
    ``criteria_routing.json`` entry (the WS2 sync invariant) — advisory, non-blocking.
  - ``overlay_union`` unions the content-triggered set in via its new ``diff_text`` input.
  - the new ``code-review-deletion-impact.md`` prompt loads with the overlay contract and is a
    canonical front-matter fixed point.
"""

from __future__ import annotations

import pathlib

import pytest

from rebar.llm.code_review import registry as reg
from rebar.llm.workflow import executor as _ex
from rebar.llm.workflow import steps as _steps  # noqa: F401 — registers the code_review ops

pytestmark = pytest.mark.unit


def _run_op(name, inputs):
    ctx = _ex.StepContext(
        run_id="r",
        step_id="s",
        kind="uses",
        step={"uses": name},
        inputs=inputs,
        workflow={},
        repo_root=None,
    )
    return _ex.STEP_REGISTRY[name](ctx)


# ── content_triggered_overlays: the polyglot removed-declaration detector ──────────────────
def test_content_trigger_fires_on_removed_python_def():
    diff = "--- a/x.py\n+++ b/x.py\n@@ -1,3 +1 @@\n-def helper(x):\n-    return x + 1\n+CONST = 1\n"
    assert reg.content_triggered_overlays(diff) == ["deletion-impact"]


def test_content_trigger_fires_on_removed_class():
    diff = "--- a/m.py\n+++ b/m.py\n@@ -1 +0,0 @@\n-class Widget:\n"
    assert reg.content_triggered_overlays(diff) == ["deletion-impact"]


def test_content_trigger_fires_on_removed_js_and_go_signatures():
    js = "--- a/a.ts\n+++ b/a.ts\n@@ -1 +0,0 @@\n-export const run = (x) => x\n"
    go = "--- a/a.go\n+++ b/a.go\n@@ -1 +0,0 @@\n-func Serve(w http.ResponseWriter) {\n"
    fn = "--- a/a.js\n+++ b/a.js\n@@ -1 +0,0 @@\n-function build(opts) {\n"
    assert reg.content_triggered_overlays(js) == ["deletion-impact"]
    assert reg.content_triggered_overlays(go) == ["deletion-impact"]
    assert reg.content_triggered_overlays(fn) == ["deletion-impact"]


def test_content_trigger_silent_on_add_only_diff():
    diff = "--- a/x.py\n+++ b/x.py\n@@ -0,0 +1,2 @@\n+def added(x):\n+    return x\n"
    assert reg.content_triggered_overlays(diff) == []


def test_content_trigger_silent_on_body_only_edit_signature_kept():
    # The `def` line is a CONTEXT line (unchanged); only the body is edited — no removed signature.
    diff = (
        "--- a/x.py\n"
        "+++ b/x.py\n"
        "@@ -1,3 +1,3 @@\n"
        " def helper(x):\n"
        "-    return x + 1\n"
        "+    return x + 2\n"
    )
    assert reg.content_triggered_overlays(diff) == []


def test_content_trigger_silent_on_removed_comment_and_blank_lines_only():
    diff = "--- a/x.py\n+++ b/x.py\n@@ -1,2 +0,0 @@\n-# a stale comment\n-\n"
    assert reg.content_triggered_overlays(diff) == []


def test_content_trigger_ignores_file_header_minus_lines():
    # `--- a/file` is a diff header, not a removed line — must never fire on it.
    diff = "--- a/def_something.py\n+++ b/def_something.py\n@@ -0,0 +1 @@\n+x = 1\n"
    assert reg.content_triggered_overlays(diff) == []


def test_content_trigger_empty_diff_is_silent():
    assert reg.content_triggered_overlays("") == []
    assert reg.content_triggered_overlays(None) == []  # type: ignore[arg-type]


# ── the sync invariant: deletion-impact ∈ OVERLAY_IDS ∧ has a routing entry ────────────────
def test_deletion_impact_is_a_registered_overlay_with_routing():
    assert "deletion-impact" in reg.OVERLAY_IDS
    idx = reg.routing_index()
    assert "deletion-impact" in idx, "deletion-impact overlay has no criteria_routing.json entry"
    entry = idx["deletion-impact"]
    assert entry["exec"] == "AGENT"
    assert entry["applies_to"] == []  # content-triggered, not glob
    assert entry["blocking_enabled"] is False  # ADVISORY — no new BLOCK source
    # advisory posture flows through threshold_for as (default, non-blocking)
    assert reg.threshold_for(["deletion-impact"]) == (0.95, False)


def test_deletion_impact_flag_key_is_underscored():
    assert reg.overlay_flag_key("deletion-impact") == "include_deletion_impact"


# ── overlay_union unions the content-triggered set in via the new diff_text input ──────────
def test_overlay_union_includes_deletion_impact_on_removed_def():
    diff = "--- a/x.py\n+++ b/x.py\n@@ -1 +0,0 @@\n-def gone(x):\n"
    out = _run_op("overlay_union", {"changed_files": ["x.py"], "diff_text": diff})
    assert "deletion-impact" in out["to_run"]
    assert out["include_deletion_impact"] is True


def test_overlay_union_no_deletion_impact_without_removed_def():
    diff = "--- a/x.py\n+++ b/x.py\n@@ -0,0 +1 @@\n+x = 1\n"
    out = _run_op("overlay_union", {"changed_files": ["x.py"], "diff_text": diff})
    assert "deletion-impact" not in out["to_run"]
    assert out["include_deletion_impact"] is False


def test_overlay_union_content_unions_with_glob_not_replaces():
    # A diff that BOTH globs to `security` (auth path) AND removes a def must run both.
    diff = (
        "--- a/src/auth/login.py\n+++ b/src/auth/login.py\n@@ -1 +0,0 @@\n-def authenticate(u):\n"
    )
    out = _run_op("overlay_union", {"changed_files": ["src/auth/login.py"], "diff_text": diff})
    assert {"security", "deletion-impact"} <= set(out["to_run"])


# ── the prompt loads with the overlay contract and is a canonical fixed point ──────────────
def test_deletion_impact_prompt_resolves_as_a_code_review_pass_finder():
    from rebar.llm.prompting.prompts import get_prompt

    p = get_prompt("code-review-deletion-impact")
    assert p.outputs == "code_review_findings"
    assert p.category == "code-review-pass"
    assert not p.is_reviewer


def test_deletion_impact_prompt_is_canonical_front_matter_fixed_point():
    from rebar.llm.prompting.prompts_frontmatter import _split_front_matter_raw, write_front_matter

    path = pathlib.Path("src/rebar/llm/reviewers/code-review-deletion-impact.md")
    text = path.read_text(encoding="utf-8")
    assert write_front_matter(*_split_front_matter_raw(text)) == text
