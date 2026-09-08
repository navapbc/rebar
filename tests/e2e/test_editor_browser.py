"""Exercise the workflow editor in browser E2E tests.

Headless Chromium exercises the built bundle through an editor server. The tests verify
rendered edges and layout, selection-driven properties, and saved IR changes. Missing browser
dependencies use the recorded opt-out guard.

This boundary catches runtime failures that syntax and Python-only checks cannot observe.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e


def test_editor_renders_diagram_panel_and_reacts_to_selection(browser_runner, editor_server):
    url, _ir = editor_server
    report = browser_runner("browser_diag.mjs", url)
    assert report["errors"] == [], f"console/page errors in the editor: {report['errors']}"

    render = report["render"]
    assert render["connectionCount"] > 0, "no sequence-flow edges rendered (the 'no arrows' bug)"
    assert render["exactOverlaps"] == 0, "shapes drawn on top of each other (the single-column bug)"

    # Selecting each step kind updates the panel and shows the editable Rebar group.
    for key, kind in (
        ("onScript", "bpmn:ScriptTask"),
        ("onAgent", "bpmn:ServiceTask"),
        ("onLoop", "bpmn:SubProcess"),
        ("onBranch", "bpmn:ExclusiveGateway"),
    ):
        assert report[key]["type"] == kind
        assert report[key]["rebarGroup"], f"Rebar properties group missing for {kind}"


def test_morph_change_type_preserves_rebar_config(browser_runner, editor_server):
    # bpmn-js is documented to drop custom extensionElements on element type-change (morph);
    # our registered moddle descriptor must keep <rebar:Config> intact so changing a step's
    # kind in the UI never loses its config. (Verified in a real browser, not just docs.)
    url, _ir = editor_server
    report = browser_runner("browser_morph.mjs", url)
    assert report["errors"] == [] if "errors" in report else True
    assert report["errs"] == [], f"errors during morph: {report['errs']}"
    assert report["type"] == "bpmn:ServiceTask"
    assert report["before"] and report["after"] == report["before"], "morph dropped rebar config"


def test_editor_edit_persists_to_ir_on_save(browser_runner, editor_server):
    url, ir = editor_server
    report = browser_runner("browser_edit.mjs", url)
    assert report["errors"] == [], f"errors during edit: {report['errors']}"
    assert "EDITED_BY_TEST" in (report["inModel"] or ""), "edit did not reach the modeler"
    assert report["status"] == "saved to IR", f"save did not succeed: {report['status']}"
    # The decisive check: the edit is in the written IR (the round-trip the user does).
    assert "EDITED_BY_TEST" in ir.read_text(encoding="utf-8")


def test_editor_structured_fields_roundtrip_error_and_no_raw_editor(browser_runner, editor_server):
    # Verify structured max_iterations editing, invalid-value retention with an error,
    # removal of the raw JSON editor, and persistence to the IR.
    import json

    url, ir = editor_server
    report = browser_runner("browser_structured.mjs", url)
    assert report["errors"] == [], f"console/page errors: {report['errors']}"
    assert report["loopId"], "no StandardLoop SubProcess found to drive structured fields"

    # (1) The structured edit wrote back into the SAME rebar:Config blob the serializer reads.
    after = json.loads(report["configAfterValid"])
    assert after["max_iterations"] == 7, f"structured field did not update the blob: {after}"

    # (2) A non-numeric value surfaces a field error and PRESERVES the prior value (7, not lost).
    assert report["invalid"]["hasError"], "non-numeric max_iterations showed no field error"
    held = json.loads(report["configAfterInvalid"])
    assert held["max_iterations"] == 7, f"invalid entry corrupted/dropped the value: {held}"

    # (3) There is NO raw JSON editor for the known kind (the structured fields are the
    # sole editor; uncommon keys round-trip via the slice-write, not a free-form textarea).
    assert not report["rawFallback"]["present"], "a raw JSON config editor is still present"

    # The final valid edit (9) persisted to the IR — the round-trip the user does.
    assert report["status"] == "saved to IR", f"save failed: {report['status']}"
    assert '"max_iterations": 9' in ir.read_text(encoding="utf-8") or (
        "max_iterations: 9" in ir.read_text(encoding="utf-8")
    ), "the structured edit did not round-trip into the reloaded IR"


def test_editor_batch_criteria_render_add_remove_edit(browser_runner, editor_server_batch):
    # Verify batch fields, criterion edits, additions, removals, prompt overlays, step
    # conversion, and persistence through the browser editor.
    import json

    url, ir = editor_server_batch
    report = browser_runner("browser_batch.mjs", url)
    assert report["errors"] == [], f"console/page errors in the editor: {report['errors']}"
    assert report["ids"]["batch"], "no batch ServiceTask found in the editor"

    # (1) RENDER: finder + budget + ladder fields, and one criteria item per criterion, with
    # the security criterion's `when` overlay visible.
    assert report["finderValue"] == "code-quality", f"finder not rendered: {report['finderValue']}"
    assert report["budgetVisible"] and report["ladderVisible"], "batch param fields missing"
    assert report["itemCountBefore"] == 2, (
        f"expected 2 criteria items, got {report['itemCountBefore']}"
    )
    assert "${{ steps.triggers.outputs.security }}" in report["whenValue"], (
        f"criterion `when` overlay not rendered: {report['whenValue']!r}"
    )

    # (2) EDIT: criterion-0's prompt was changed in the rebar:Config blob (the serializer's source).
    edited = json.loads(report["configAfterEdit"])
    assert edited["batch"]["criteria"][0]["prompt"] == "ticket-quality", (
        f"criterion edit did not write back: {edited['batch']['criteria']}"
    )

    # (3) ADD: a new (empty) criterion appears in both the config and the rendered list.
    added = json.loads(report["configAfterAdd"])
    assert len(added["batch"]["criteria"]) == 3, (
        f"add did not grow the list: {added['batch']['criteria']}"
    )
    assert report["itemCountAfterAdd"] == 3

    # (4) REMOVE: the list shrinks back by one in both config and UI.
    removed = json.loads(report["configAfterRemove"])
    assert len(removed["batch"]["criteria"]) == 2, (
        f"remove did not shrink the list: {removed['batch']['criteria']}"
    )
    assert report["itemCountAfterRemove"] == 2

    # (5) OVERLAY: the `if:` predicate field renders + reads for a prompt (agent) step.
    assert report["ifFieldPresent"], "no if: overlay field on the prompt step"
    assert "${{ inputs.notify_enabled }}" in report["ifValue"], (
        f"if value wrong: {report['ifValue']!r}"
    )

    # (5b) CREATE: the ServiceTask kind toggle converts an agent step INTO a batch step
    # (seeds cfg.batch) and back (drops it), so a batch can be authored from scratch.
    assert report["kindTogglePresent"], "no ServiceTask kind (agent/batch) toggle"
    converted = json.loads(report["overlayConfigAfterConvert"])
    assert isinstance(converted.get("batch"), dict), (
        f"convert→batch did not seed cfg.batch: {converted}"
    )
    reverted = json.loads(report["overlayConfigAfterRevert"])
    assert "batch" not in reverted, f"convert→agent did not drop cfg.batch: {reverted}"

    # (6) SAVE persisted to the IR — the criterion edit round-trips through the reloaded file.
    assert report["status"] == "saved to IR", f"save failed: {report['status']}"
    assert "ticket-quality" in ir.read_text(encoding="utf-8"), (
        "the criterion edit did not reach the IR"
    )


def test_editor_live_validation_error_clear_and_unavailable(browser_runner, editor_server):
    # Story 998e: live config validation drives a red inline error region, a valid config
    # CLEARS it, and a 500 from /validate surfaces the DISTINCT "validation unavailable"
    # banner (never false-valid). Save is BLOCKED while errors exist OR while unavailable.
    url, _ir = editor_server
    report = browser_runner("browser_validate.mjs", url)
    assert report["errors"] == [], f"console/page errors in the editor: {report['errors']}"

    # INVALID → red error region visible, Save blocked.
    inv = report["invalid"]
    assert inv["visible"] and inv["cls"] == "rebar-validate-errors"
    assert inv["saveDisabled"] is True and inv["state"] == "errors"

    # VALID → error cleared, Save re-enabled.
    val = report["valid"]
    assert val["hidden"] and val["saveDisabled"] is False and val["state"] == "valid"

    # UNAVAILABLE (a 500 from /validate) → amber banner, Save blocked, NOT rendered valid.
    una = report["unavailable"]
    assert una["visible"] and una["cls"] == "rebar-validate-unavailable"
    assert una["saveDisabled"] is True and una["state"] == "unavailable"
    assert "validation unavailable" in una["text"]

    # The per-kind help panel rendered the element types + shapes.
    assert report["help"]["present"]
    for kind in ("scripted", "agent", "branch", "loop", "map"):
        assert kind in report["help"]["text"]
