"""Ticket restoring-shallow-blobfish: the GLOB-triggered ``surface-parity`` code-review overlay.

Pins, OFFLINE (no tokens):
  - ``surface-parity`` is a member of the closed ``OVERLAY_IDS`` enum AND has a
    ``criteria_routing.json`` entry — GLOB-triggered on the write-op adapter files, advisory,
    non-blocking.
  - ``overlay_union`` fires ``surface-parity`` when a diff touches a CLI ``_commands/*`` file,
    ``_lib_writes.py``, or ``_mcp_writes.py``, and stays inert on an unrelated file.
  - the new ``code-review-surface-parity.md`` prompt loads with the overlay finder contract and
    is a canonical front-matter fixed point.
  - the base-reviewer overlay catalog lists ``surface-parity`` (drift parity with OVERLAY_IDS).
  - the finder is actually DISPATCHED — wired into the round_a AND round_b batches of the gate.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

from rebar.llm.code_review import registry as reg
from rebar.llm.workflow import executor as _ex
from rebar.llm.workflow import steps as _steps  # noqa: F401 — registers the code_review ops

pytestmark = pytest.mark.unit

_GATE = pathlib.Path("src/rebar/llm/workflow/gates/code-review.yaml")


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


# ── the sync invariant: surface-parity ∈ OVERLAY_IDS ∧ has a GLOB routing entry ────────────
def test_surface_parity_is_a_registered_overlay_with_glob_routing():
    assert "surface-parity" in reg.OVERLAY_IDS
    idx = reg.routing_index()
    assert "surface-parity" in idx, "surface-parity overlay has no criteria_routing.json entry"
    entry = idx["surface-parity"]
    assert entry["exec"] == "AGENT"
    # GLOB-triggered (unlike deletion-impact/scope-intent which carry an empty applies_to)
    assert set(entry["applies_to"]) == {
        "**/_commands/**",
        "**/_lib_writes.py",
        "**/_mcp_writes.py",
    }
    assert entry["default_posture"] == "advisory"
    assert entry["blocking_enabled"] is False  # ADVISORY — no new BLOCK source
    # advisory posture flows through threshold_for as (default, non-blocking); it does NOT join
    # the approved blocking set.
    assert reg.threshold_for(["surface-parity"]) == (0.95, False)


def test_surface_parity_flag_key_is_underscored():
    assert reg.overlay_flag_key("surface-parity") == "include_surface_parity"


# ── overlay_union fires the overlay on a changed write-op adapter file (glob), else inert ───
@pytest.mark.parametrize(
    "changed",
    [
        "src/rebar/_commands/transition.py",
        "src/rebar/_commands/claim.py",
        "src/rebar/_lib_writes.py",
        "src/rebar/_mcp_writes.py",
    ],
)
def test_overlay_union_fires_surface_parity_on_write_adapter(changed):
    out = _run_op("overlay_union", {"changed_files": [changed]})
    assert "surface-parity" in out["to_run"]
    assert out["include_surface_parity"] is True


def test_overlay_union_inert_on_unrelated_file():
    out = _run_op("overlay_union", {"changed_files": ["docs/x.md", "README.md"]})
    assert "surface-parity" not in out["to_run"]
    assert out["include_surface_parity"] is False


# ── the prompt loads with the overlay finder contract and is a canonical fixed point ───────
def test_surface_parity_prompt_resolves_as_a_code_review_pass_finder():
    from rebar.llm.prompting.prompts import get_prompt

    p = get_prompt("code-review-surface-parity")
    assert p.outputs == "code_review_findings"
    assert p.category == "code-review-pass"
    assert not p.is_reviewer


def test_surface_parity_prompt_is_canonical_front_matter_fixed_point():
    from rebar.llm.prompting.prompts_frontmatter import _split_front_matter_raw, write_front_matter

    path = pathlib.Path("src/rebar/llm/reviewers/code-review-surface-parity.md")
    text = path.read_text(encoding="utf-8")
    assert write_front_matter(*_split_front_matter_raw(text)) == text


# ── the base-reviewer catalog lists surface-parity (drift parity with OVERLAY_IDS) ─────────
def test_base_reviewer_catalog_lists_surface_parity():
    import re

    body = pathlib.Path("src/rebar/llm/reviewers/code-review-base.md").read_text()
    listed = set(re.findall(r"^- `([a-z0-9-]+)` —", body, flags=re.MULTILINE))
    assert "surface-parity" in listed
    # the whole catalog stays EXACTLY the OVERLAY_IDS set (the no-drift invariant).
    assert listed == set(reg.OVERLAY_IDS)


# ── the finder is actually DISPATCHED — wired into both gate batches ────────────────────────
def test_surface_parity_finder_dispatched_in_round_a_and_round_b():
    doc = yaml.safe_load(_GATE.read_text())
    by_id = {s["id"]: s for s in doc["steps"]}
    round_a = {c["prompt"]: c["when"] for c in by_id["round_a"]["batch"]["criteria"]}
    round_b = {c["prompt"]: c["when"] for c in by_id["round_b"]["batch"]["criteria"]}
    assert (
        round_a.get("code-review-surface-parity")
        == "${{ steps.triggers.outputs.include_surface_parity }}"
    )
    assert (
        round_b.get("code-review-surface-parity")
        == "${{ steps.union.outputs.include_surface_parity }}"
    )
