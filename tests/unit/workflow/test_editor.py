"""Ephemeral bpmn-js visual editor (744b): the host page, the BPMN->IR save
round-trip, and the loopback edit server — all offline (the in-browser visual editing
itself is human-validated). The visual format is NEVER written to git.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from rebar.llm.workflow import bpmn, editor


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
    from rebar.llm.workflow.schema import dump_workflow

    p = tmp_path / "demo.yaml"
    p.write_text(dump_workflow(doc), encoding="utf-8")
    return p


# ── The bpmn-js host page ──────────────────────────────────────────────────────


def test_host_html_embeds_the_integration_contract():
    # Pin the INTEGRATION CONTRACT the browser bundle depends on (not template internals):
    # the host shell loads the vendored bundle and hands it three globals — the rebar
    # moddle descriptor (extension survival), the diagram to edit, and the per-session
    # token. The editor logic (Modeler/panel/auto-layout/Save) lives in the bundle.
    xml = bpmn.ir_to_bpmn({"schema_version": "2", "name": "x", "steps": [{"id": "a", "uses": "o"}]})
    html = editor.build_host_html(xml, token="tok", prompts={"review-ticket": "do the review"})
    assert "/assets/editor.js" in html and "/assets/editor.css" in html  # loads the bundle
    assert "REBAR_MODDLE" in html and bpmn.REBAR in html  # descriptor injected
    assert "REBAR_DIAGRAM" in html and "process id" in html  # the diagram XML is embedded
    assert "REBAR_TOKEN" in html and "tok" in html  # token handed to the bundle for /save
    assert "REBAR_PROMPTS" in html and "do the review" in html  # prompt text for display


def test_served_assets_are_allow_listed():
    # Only the two known bundle files are served; an arbitrary name (path traversal etc.)
    # returns nothing. Skips if the bundle has not been built in this checkout.
    if not editor.assets_available():
        import pytest as _pytest

        _pytest.skip("editor bundle not built (run editor_assets npm build)")
    assert editor.read_asset("editor.js") is not None
    assert editor.read_asset("editor.css") is not None
    assert editor.read_asset("../editor.py") is None  # no traversal
    assert editor.read_asset("nope.js") is None


def test_built_bundle_carries_structured_field_paths():
    # Story a83a: the properties panel now renders STRUCTURED per-field entries for the
    # common path (with the raw JSON editor kept as a fallback). The faithful oracle is the
    # browser tier (tests/e2e/test_editor_browser.py), but as an always-on floor assert the
    # built bundle actually carries the structured-field, raw-fallback, and field-validation
    # code paths — so a build that dropped them can't pass silently. Skips if not built.
    if not editor.assets_available():
        import pytest as _pytest

        _pytest.skip("editor bundle not built (run editor_assets npm build)")
    js = editor.read_asset("editor.js") or b""
    text = js.decode("utf-8", "replace")
    # Structured fields (per-kind labels + entry ids surfaced into the DOM).
    assert "max_iterations" in text and "max_concurrency" in text and "index_var" in text
    assert "rebar-config-advanced" in text  # the structured group references the raw fallback
    assert "Advanced (raw JSON)" in text  # the raw-JSON fallback entry label
    # Field-level validation messaging (the "shows an error, never silent loss" path).
    assert "Must be a number" in text


# ── BPMN -> IR save round-trip ─────────────────────────────────────────────────


def test_save_writes_ir_and_round_trips(tmp_path):
    path = _wf_file(tmp_path)
    original = path.read_text(encoding="utf-8")
    # An "edit": re-serialize the current IR to BPMN (a no-op visual round-trip) and save.
    xml = editor._load_bpmn_for(path)
    errors = editor.save_bpmn_to_ir(xml, path)
    assert errors == []
    # The IR file was (re)written and still parses to the same logical workflow.
    from rebar.llm.workflow.schema import parse_workflow

    assert parse_workflow(path.read_text(encoding="utf-8"))["steps"][1]["branch"]["when"] == (
        "${{ steps.start.outputs.ok }}"
    )
    # No visual artifact committed: only the .yaml exists (no .bpmn alongside it).
    assert not list(tmp_path.glob("*.bpmn"))
    assert original  # sanity


def test_save_rejects_unmappable_edit_and_leaves_file_untouched(tmp_path):
    path = _wf_file(tmp_path)
    before = path.read_text(encoding="utf-8")
    # A bare sub-process (no loop characteristics) is a shape the IR can't express.
    bad = (
        '<?xml version="1.0"?><bpmn:definitions '
        'xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL">'
        '<bpmn:process id="p"><bpmn:subProcess id="sp"/></bpmn:process></bpmn:definitions>'
    )
    errors = editor.save_bpmn_to_ir(bad, path)
    assert errors and any("does not map" in e or "characteristics" in e for e in errors)
    assert path.read_text(encoding="utf-8") == before  # file untouched on a rejected edit


# ── The loopback edit server (no browser) ──────────────────────────────────────


@pytest.fixture
def _server(tmp_path):
    path = _wf_file(tmp_path)
    server, host, port, token = editor.edit_workflow(
        path, open_browser=False, serve_forever=False, host="127.0.0.1"
    )
    yield path, f"http://{host}:{port}", token
    server.shutdown()
    server.server_close()


def test_server_binds_loopback_only(_server):
    _path, base, _token = _server
    assert base.startswith("http://127.0.0.1:")  # never a public interface (it can write)


def _save(base, token, xml):
    req = urllib.request.Request(
        base + "/save",
        data=xml.encode("utf-8"),
        method="POST",
        headers={"X-Rebar-Token": token},
    )
    return urllib.request.urlopen(req)


@pytest.mark.allow_network  # loopback only (127.0.0.1) — the edit server is local
def test_server_serves_host_and_bpmn(_server):
    _path, base, _token = _server
    html = urllib.request.urlopen(base + "/").read().decode("utf-8")
    assert "/assets/editor.js" in html and "REBAR_DIAGRAM" in html  # the host page's contract
    xml = urllib.request.urlopen(base + "/workflow.bpmn").read().decode("utf-8")
    assert "bpmn:process" in xml and "rebar:" in xml
    # the vendored bundle is served locally (no CDN) with the right content types
    js = urllib.request.urlopen(base + "/assets/editor.js")
    assert js.status == 200 and "javascript" in js.headers.get("Content-Type", "")
    css = urllib.request.urlopen(base + "/assets/editor.css")
    assert css.status == 200 and "css" in css.headers.get("Content-Type", "")


@pytest.mark.allow_network  # loopback only
def test_server_save_round_trips_and_backs_up(_server):
    path, base, token = _server
    xml = urllib.request.urlopen(base + "/workflow.bpmn").read().decode("utf-8")
    resp = _save(base, token, xml)
    assert resp.status == 200 and json.loads(resp.read())["ok"] is True
    assert "schema_version" in path.read_text(encoding="utf-8")
    # M2: the prior IR is backed up before overwrite (comments are recoverable).
    assert path.with_suffix(".yaml.bak").is_file()


@pytest.mark.allow_network  # loopback only
def test_server_rejects_save_without_token(_server):
    # CSRF guard (M1): a POST without the per-session token is forbidden — a
    # cross-origin page can't read the host HTML to learn it.
    path, base, _token = _server
    before = path.read_text(encoding="utf-8")
    xml = urllib.request.urlopen(base + "/workflow.bpmn").read().decode("utf-8")
    req = urllib.request.Request(base + "/save", data=xml.encode("utf-8"), method="POST")
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req)
    assert exc.value.code == 403
    assert path.read_text(encoding="utf-8") == before  # not overwritten


@pytest.mark.allow_network  # loopback only
def test_server_rejects_invalid_save(_server):
    path, base, token = _server
    before = path.read_text(encoding="utf-8")
    bad = (
        '<?xml version="1.0"?><bpmn:definitions '
        'xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL">'
        '<bpmn:process id="p"><bpmn:subProcess id="sp"/></bpmn:process></bpmn:definitions>'
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        _save(base, token, bad)
    assert exc.value.code == 422
    assert json.loads(exc.value.read())["errors"]
    assert path.read_text(encoding="utf-8") == before  # untouched


def test_save_rejects_xxe_external_entity(tmp_path):
    # The save parse must not resolve external entities (untrusted POST input). stdlib
    # ElementTree blocks this; assert it so a future change can't silently reopen it.
    path = _wf_file(tmp_path)
    before = path.read_text(encoding="utf-8")
    xxe = (
        '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]>'
        '<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL">'
        '<bpmn:process id="&e;"/></bpmn:definitions>'
    )
    errors = editor.save_bpmn_to_ir(xxe, path)
    assert errors  # rejected, not parsed/written
    assert path.read_text(encoding="utf-8") == before


def test_sequential_saves_each_back_up_the_prior_ir(tmp_path):
    # Two saves in a row both succeed; the .bak after the second reflects the state
    # written by the FIRST (the backup is taken before each overwrite, so the prior IR
    # is always recoverable).
    path = _wf_file(tmp_path)
    bak = path.with_suffix(".yaml.bak")
    xml = editor._load_bpmn_for(path)
    assert editor.save_bpmn_to_ir(xml, path) == []
    first_saved = path.read_text(encoding="utf-8")
    # A second save (re-serialize the now-saved IR) backs up `first_saved`.
    assert editor.save_bpmn_to_ir(editor._load_bpmn_for(path), path) == []
    assert bak.read_text(encoding="utf-8") == first_saved
