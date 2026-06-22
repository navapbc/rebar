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


def test_editor_edit_persists_to_ir_on_save(browser_runner, editor_server):
    url, ir = editor_server
    report = browser_runner("browser_edit.mjs", url)
    assert report["errors"] == [], f"errors during edit: {report['errors']}"
    assert "EDITED_BY_TEST" in (report["inModel"] or ""), "edit did not reach the modeler"
    assert report["status"] == "saved to IR", f"save did not succeed: {report['status']}"
    # The decisive check: the edit is in the written IR (the round-trip the user does).
    assert "EDITED_BY_TEST" in ir.read_text(encoding="utf-8")
