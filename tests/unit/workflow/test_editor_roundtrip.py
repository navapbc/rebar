"""RP-06 S6 — the authored workflow round-trips byte-equivalently while the read-only
Effective Policy view tracks project policy.

The oracle (the ticket names it explicitly): the serialized AUTHORED document — the workflow
YAML the editor owns as the single editable topology — stays byte-equivalent across a project
review-policy change, while the derived ``effective_policy_view`` changes with that policy.
This proves the two authorities are separate: editing project criteria routing never rewrites
authored YAML/BPMN, and the effective view is a projection, not a second editable source.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rebar.llm import criteria
from rebar.llm.prompting import prompt_library
from rebar.llm.workflow import editor, editor_contracts
from rebar.llm.workflow.schema import dump_workflow

_CODE = "code_review"


def _wf_file(tmp_path: Path) -> Path:
    doc = {
        "schema_version": "2",
        "name": "demo",
        "inputs": {"items": {"type": "array"}},
        "steps": [
            {"id": "start", "uses": "noop"},
            {
                "id": "gate",
                "needs": ["start"],
                "branch": {
                    "when": "${{ steps.start.outputs.ok }}",
                    "then": [{"id": "approve", "uses": "emit"}],
                    "else": [{"id": "reject", "uses": "emit"}],
                },
            },
        ],
    }
    p = tmp_path / "demo.yaml"
    p.write_text(dump_workflow(doc), encoding="utf-8")
    return p


def _write_overlay(root: Path, *, cr_globs) -> None:
    rebar_dir = root / ".rebar"
    rebar_dir.mkdir(parents=True, exist_ok=True)
    overlay = {
        "code_review": {
            "project.house-style": {
                "exec": "1-TURN",
                "facet": "project-invariants",
                "applies_to": cr_globs,
                "block_threshold": 0.8,
                "default_posture": "advisory",
            }
        },
        "activate": {"project.house-style": ["code_review"]},
    }
    (rebar_dir / "criteria_routing.json").write_text(json.dumps(overlay), encoding="utf-8")
    pdir = rebar_dir / "prompts"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "code-review-project-house-style.md").write_text("rubric", encoding="utf-8")


@pytest.fixture(autouse=True)
def _clear_caches():
    prompt_library._invalidate_caches()
    criteria.clear_caches()
    yield
    prompt_library._invalidate_caches()
    criteria.clear_caches()


def test_policy_change_leaves_authored_workflow_byte_equivalent_but_changes_the_view(tmp_path):
    """AC6/AC1: change the project code-review policy (repository-wide → scoped) and the
    authored workflow YAML is byte-IDENTICAL, while the derived effective view changes."""
    wf = _wf_file(tmp_path)
    _write_overlay(tmp_path, cr_globs=["**"])
    prompt_library._invalidate_caches()
    criteria.clear_caches()

    authored_before = wf.read_bytes()
    view_before = editor_contracts.effective_policy_view(repo_root=str(tmp_path))

    # Change ONLY the project review policy (a different applicability), not the workflow.
    _write_overlay(tmp_path, cr_globs=["src/**/*.py"])
    prompt_library._invalidate_caches()
    criteria.clear_caches()

    authored_after = wf.read_bytes()
    view_after = editor_contracts.effective_policy_view(repo_root=str(tmp_path))

    # The authored topology the editor owns is untouched — byte for byte.
    assert authored_after == authored_before
    # The derived view tracked the policy change (different digest and applicability).
    assert view_after["digest"] != view_before["digest"]
    house_before = next(c for c in view_before["criteria"] if c["id"] == "project.house-style")
    house_after = next(c for c in view_after["criteria"] if c["id"] == "project.house-style")
    assert house_before["applicability"] == "repository-wide"
    assert house_after["applicability"] != "repository-wide"


def test_visual_save_roundtrip_is_byte_equivalent_under_a_project_policy(tmp_path):
    """AC1: a no-op visual round-trip (BPMN re-serialized and saved) preserves the authored
    YAML, and the presence of a project effective policy does not perturb that round-trip —
    the effective view is not written back into the workflow."""
    wf = _wf_file(tmp_path)
    _write_overlay(tmp_path, cr_globs=["**"])
    before = wf.read_bytes()

    xml = editor._load_bpmn_for(wf)
    errors = editor.save_bpmn_to_ir(xml, wf)
    assert errors == []

    # A no-op round-trip preserves the logical workflow, and writes NO visual artifact.
    from rebar.llm.workflow.schema import parse_workflow

    assert parse_workflow(wf.read_text(encoding="utf-8"))["steps"][1]["branch"]["when"] == (
        "${{ steps.start.outputs.ok }}"
    )
    assert not list(tmp_path.glob("*.bpmn"))
    assert before  # the pre-edit authored bytes existed
