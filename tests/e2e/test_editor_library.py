"""Browser E2E for the prompt LIBRARY + typed INSERTION + create/edit (story 6592).

Drives the REAL bundle in headless Chromium against a live editor server: the library
mounts and lists prompts, the typed insertion produces a valid scripted-op step
(`uses:` → ScriptTask) AND a prompt step (`prompt:` → ServiceTask), and creating a new
prompt via the form POSTs `/prompt/save` and persists a `.rebar/prompts/<id>.md`.

Self-skips when Node/Playwright/Chromium or the built bundle are unavailable (same
contract as the other ``tests/e2e/test_editor_*`` tiers) — the Python unit suite in
``tests/unit/workflow/test_prompt_authoring.py`` is the always-on verification floor.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e


@pytest.fixture
def library_editor_server(tmp_path):
    """An editor server whose repo_root is a writable PROJECT dir (so prompt writes
    land in tmp's ``.rebar/prompts`` rather than the real source tree)."""
    from rebar.llm.workflow import editor as _editor

    sample = Path(__file__).parent / "fixtures" / "roundtrip-demo.yaml"
    if not sample.is_file() or not _editor.assets_available():
        pytest.skip("e2e(browser): fixture workflow or built editor bundle missing")
    repo = tmp_path
    (repo / ".rebar" / "prompts").mkdir(parents=True)
    ir = repo / "roundtrip-demo.yaml"
    shutil.copy(sample, ir)

    import rebar.config as _cfg

    orig = _cfg.repo_root
    _cfg.repo_root = lambda: repo  # type: ignore[assignment]
    server, host, port, _token = _editor.edit_workflow(
        ir, open_browser=False, serve_forever=False, host="127.0.0.1"
    )
    try:
        yield f"http://{host}:{port}/", repo
    finally:
        _cfg.repo_root = orig  # type: ignore[assignment]
        server.shutdown()
        server.server_close()


def test_library_insert_create_edit(browser_runner, library_editor_server):
    url, repo = library_editor_server
    report = browser_runner("browser_library.mjs", url)
    assert report["errors"] == [], f"console/page errors in the library: {report['errors']}"
    assert report["promptCount"] > 0, "the library listed no prompts from /prompts"

    # Typed insertion produced the right bpmn kinds + names (the round-trip contract).
    assert report["inserted"]["op"]["type"] == "bpmn:ScriptTask"
    assert report["inserted"]["op"]["name"] == "noop"  # → uses: noop
    assert report["inserted"]["prompt"]["type"] == "bpmn:ServiceTask"
    assert report["inserted"]["prompt"]["name"]  # → prompt: <id>

    # Create-via-form persisted a project override (POST /prompt/save succeeded).
    assert "saved" in report["saveStatus"], f"create did not save: {report['saveStatus']}"
    assert (repo / ".rebar" / "prompts" / "e2e-created.md").is_file()
    assert report["libOptionCount"] > 1
