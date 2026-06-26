"""Browser E2E for the library-backed batch criteria editing (story B-UX).

Drives the REAL bundle in headless Chromium against a live editor server whose repo_root is
a writable PROJECT tmp dir (so an authored prompt lands in tmp's ``.rebar/prompts`` rather
than the real source tree). Proves the four B-UX guarantees the editor must now offer on a
batch step's criteria — instead of free-text typing:

  (a) the criterion ``prompt`` field is a SELECT of library options (window.REBAR_LIBRARY);
  (b) selecting an existing library id persists into the criterion's rebar:Config;
  (c) authoring a NEW overlay trigger from the ``when`` dropdown sets the criterion's ``when``
      to the full ``${{ steps.<id>.outputs.<name> }}`` expression (and writes the trigger
      onto the overlay_triggers step);
  (d) authoring a NEW criterion via the prompt "➕ Create new…" form POSTs /library/create,
      writes ``.rebar/prompts/<id>.md`` under the server's repo_root, and references the new
      id on the criterion.

Self-skips when Node/Playwright/Chromium or the built bundle are unavailable (same contract
as the other ``tests/e2e/test_editor_*`` tiers); the always-on verification floor is the
Python unit suite for ``prompt_library`` + ``editor``.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e


@pytest.fixture
def batch_library_editor_server(tmp_path):
    """An editor server on the batch-demo fixture whose repo_root is a writable PROJECT dir,
    so an authored criterion/prompt is written to tmp's ``.rebar/prompts`` (not the real tree)."""
    from rebar.llm.workflow import editor as _editor

    sample = Path(__file__).parent / "fixtures" / "batch-demo.yaml"
    if not sample.is_file() or not _editor.assets_available():
        pytest.skip("e2e(browser): batch fixture workflow or built editor bundle missing")
    repo = tmp_path
    (repo / ".rebar" / "prompts").mkdir(parents=True)
    ir = repo / "batch-demo.yaml"
    shutil.copy(sample, ir)

    import rebar.config as _cfg

    orig = _cfg.repo_root
    _cfg.repo_root = lambda explicit=None: repo  # type: ignore[assignment]
    server, host, port, _token = _editor.edit_workflow(
        ir, open_browser=False, serve_forever=False, host="127.0.0.1"
    )
    try:
        yield f"http://{host}:{port}/", repo, ir
    finally:
        _cfg.repo_root = orig  # type: ignore[assignment]
        server.shutdown()
        server.server_close()


def test_editor_batch_library_select_and_author(browser_runner, batch_library_editor_server):
    url, repo, ir = batch_library_editor_server
    report = browser_runner("browser_batch_library.mjs", url)
    assert report["errors"] == [], f"console/page errors in the editor: {report['errors']}"
    assert report["ids"]["batch"], "no batch ServiceTask found in the editor"

    # (a) RENDER: the criterion prompt field is a SELECT whose options are the library entries
    # (value=id) plus the "➕ Create new…" sentinel — no free-text input.
    assert report["promptIsSelect"] == "select", (
        f"criterion prompt is not a dropdown: {report['promptIsSelect']!r}"
    )
    opts = report["promptOptions"]
    assert "__rebar_create__" in opts, "the '➕ Create new…' sentinel option is missing"
    for lib_id in ("tests", "ticket-quality", "security"):
        assert lib_id in opts, f"library id {lib_id!r} not offered in the prompt dropdown: {opts}"

    # (b) SELECT: picking an existing library id persists into the criterion's rebar:Config.
    selected = json.loads(report["configAfterSelect"])
    assert selected["batch"]["criteria"][1]["prompt"] == "ticket-quality", (
        f"selecting an existing criterion did not persist: {selected['batch']['criteria']}"
    )

    # (c) TRIGGER: authoring a new overlay trigger set the criterion's `when` to the full
    # ${{ steps... }} expression AND wrote the keyword trigger onto the overlay_triggers step.
    triggered = json.loads(report["configAfterTrigger"])
    assert triggered["batch"]["criteria"][1]["when"] == "${{ steps.triggers.outputs.perf }}", (
        f"new-trigger selection did not set the when expression: {triggered['batch']['criteria']}"
    )
    trig_cfg = json.loads(report["triggersConfig"])
    assert trig_cfg["with"]["keyword_triggers"]["perf"] == ["latency", "slow"], (
        f"the new keyword trigger was not written to the overlay_triggers step: {trig_cfg}"
    )

    # (d) AUTHOR: the new criterion is referenced on the criterion AND its prompt-library file
    # was written under the server's repo_root (the temp project), keeping the drift gate green.
    new_id = report["newId"]
    authored = json.loads(report["configAfterAuthor"])
    assert authored["batch"]["criteria"][0]["prompt"] == new_id, (
        f"authored criterion id not referenced on the step: {authored['batch']['criteria']}"
    )
    written = repo / ".rebar" / "prompts" / f"{new_id}.md"
    assert written.is_file(), f"create_prompt did not write {written} under repo_root"
    body = written.read_text(encoding="utf-8")
    assert "Check the new thing is correct." in body, f"authored body not persisted: {body!r}"
    assert "category: plan-review-criterion" in body, f"criterion category not stamped: {body!r}"

    # SAVE round-tripped the edits into the reloaded IR (the round-trip the user does).
    assert report["status"] == "saved to IR", f"save failed: {report['status']}"
    ir_text = ir.read_text(encoding="utf-8")
    assert new_id in ir_text and "ticket-quality" in ir_text, (
        "the library/authoring edits did not reach the IR"
    )
