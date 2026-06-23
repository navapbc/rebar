"""Real-browser E2E for the workflow visual editor.

These run the ACTUAL editor bundle in headless Chromium (Playwright) against a live editor
server, because the failures that matter here are runtime ones a Python/headless check
can't see: whether edges render, whether the diagram is laid out (not a single column),
whether the properties panel reacts to selection, and whether an edit actually persists to
the IR on Save. Self-skips when Node/Playwright/Chromium are unavailable.

This tier exists because earlier "verified" editor changes shipped broken — the bundle
syntax-checked but threw at render time. The browser is the only faithful oracle.
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
