"""Ephemeral bpmn-js visual editing (744b) — view + edit a workflow IR visually.

Delivers the first-order requirement: a human opens a workflow's IR in a CONSTRAINED
visual editor (bpmn-js), edits the flow/steps/loops/conditionals, and saves back to
the IR — WITHOUT committing any visual artifact. The pieces:

  * **bpmn-js host** (:func:`build_host_html`) — a self-contained editor page embedding
    bpmn-js's standard *Modeler* (whose palette offers ONLY BPMN elements, so edits are
    constrained to the BPMN metamodel — unlike a free-form draw.io canvas) and the
    registered ``rebar`` moddle descriptor (so the extension elements survive
    save/copy). It opens IR->BPMN, and on Save serializes + POSTs the BPMN back.
  * **edit-time launcher** (:func:`edit_workflow`) — loads the workflow IR, serializes
    it to BPMN (the 00da serializer), starts a LOCAL, loopback-only HTTP server
    (stdlib ``http.server`` — no new runtime dependency), opens the browser, and on
    Save round-trips BPMN->IR and writes ONLY the IR file (the BPMN is ephemeral, held
    in memory, never written to git). An invalid edit (a shape the IR can't express) is
    rejected with located errors and the file is left untouched.

Edit-time only + out-of-process: bpmn-js runs in the BROWSER (loaded from a CDN); the
Python side is stdlib + the existing serializer, so the runtime/client surface is
unaffected. The round-trip logic is factored into pure functions so it is testable
without a browser; the human validates the visual interaction.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .bpmn import REBAR_MODDLE_DESCRIPTOR, bpmn_to_ir, ir_to_bpmn
from .lint import lint_workflow
from .schema import dump_workflow, load_workflow

# bpmn-js from a CDN — an EDIT-TIME, in-browser dependency only (never a runtime/client
# dep). Pinned so the editor is reproducible.
_BPMN_JS_VERSION = "17.11.1"
_BPMN_JS_CDN = f"https://unpkg.com/bpmn-js@{_BPMN_JS_VERSION}/dist"


def build_host_html(
    bpmn_xml: str, *, token: str = "", descriptor: dict[str, Any] | None = None
) -> str:
    """The self-contained bpmn-js editor page. Embeds the BPMN to edit + the ``rebar``
    moddle descriptor (so extension elements survive), and POSTs the edited BPMN to
    ``/save`` on Save. Uses the standard Modeler — its palette is BPMN-only, so a human
    cannot draw a shape that does not map back to the IR. The per-session ``token`` is
    embedded here and sent on Save; a cross-origin page cannot READ this page (so cannot
    learn the token), which closes the CSRF/DNS-rebinding write vector on ``/save``."""
    desc = json.dumps(descriptor or REBAR_MODDLE_DESCRIPTOR)
    diagram = json.dumps(bpmn_xml)
    tok = json.dumps(token)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <title>rebar — visual workflow editor</title>
  <link rel="stylesheet" href="{_BPMN_JS_CDN}/assets/diagram-js.css"/>
  <link rel="stylesheet" href="{_BPMN_JS_CDN}/assets/bpmn-js.css"/>
  <link rel="stylesheet" href="{_BPMN_JS_CDN}/assets/bpmn-font/css/bpmn.css"/>
  <style>
    html, body {{ height: 100%; margin: 0; font-family: system-ui, sans-serif; }}
    #canvas {{ height: calc(100% - 44px); }}
    #bar {{ height: 44px; display: flex; align-items: center; gap: 12px; padding: 0 12px;
            background: #1f2430; color: #e6e6e6; }}
    #bar button {{ padding: 6px 14px; cursor: pointer; }}
    #status {{ margin-left: auto; font-size: 13px; }}
    .ok {{ color: #7ee787; }} .err {{ color: #ff7b72; }}
  </style>
</head>
<body>
  <div id="bar">
    <strong>rebar</strong> visual workflow editor
    <button id="save">Save to IR</button>
    <span id="status">edits stay in the BPMN metamodel; only the IR file is written</span>
  </div>
  <div id="canvas"></div>
  <script src="{_BPMN_JS_CDN}/bpmn-modeler.development.js"></script>
  <script>
    const REBAR_MODDLE = {desc};
    const DIAGRAM = {diagram};
    const TOKEN = {tok};
    const modeler = new BpmnJS({{
      container: '#canvas',
      moddleExtensions: {{ rebar: REBAR_MODDLE }}   // POC: descriptor -> extensions survive
    }});
    const status = document.getElementById('status');
    function setStatus(msg, cls) {{ status.textContent = msg; status.className = cls; }}
    modeler.importXML(DIAGRAM).then(() => modeler.get('canvas').zoom('fit-viewport'))
      .catch(e => setStatus('open failed: ' + e.message, 'err'));
    document.getElementById('save').addEventListener('click', async () => {{
      try {{
        const {{ xml }} = await modeler.saveXML({{ format: true }});
        const r = await fetch('/save', {{
          method: 'POST', body: xml, headers: {{ 'X-Rebar-Token': TOKEN }}
        }});
        const body = await r.json();
        if (r.ok) setStatus('saved to IR', 'ok');
        else setStatus('rejected: ' + (body.errors || []).join('; '), 'err');
      }} catch (e) {{ setStatus('save failed: ' + e.message, 'err'); }}
    }});
  </script>
</body>
</html>"""


def save_bpmn_to_ir(bpmn_xml: str, path: str | Path) -> list[str]:
    """Round-trip edited BPMN back to the workflow IR FILE at ``path``.

    BPMN -> IR (the 00da serializer) -> validate + lint -> write ONLY the IR file (never
    the BPMN). Returns an EMPTY list on success; a non-empty list of located errors when
    the edit produced something the IR can't express (e.g. a bare sub-process / an
    un-mappable shape) — in which case the file is LEFT UNTOUCHED (the editor shows the
    errors). This is what keeps a visual edit inside the IR's vocabulary."""
    try:
        doc = bpmn_to_ir(bpmn_xml)
    except Exception as exc:  # noqa: BLE001 - any parse/mapping failure is a rejected edit
        return [f"the edited diagram does not map to a valid workflow: {exc}"]
    text = dump_workflow(doc)
    errors = [str(f) for f in lint_workflow(text) if f.severity == "error"]
    if errors:
        return errors  # invalid IR — do NOT write; surface to the editor
    target = Path(path)
    # Back up the prior IR before overwriting. The IR has no representation for YAML
    # COMMENTS, so a Save necessarily drops them (dump_workflow re-emits the parsed
    # dict) — the .bak keeps any annotations recoverable rather than silently lost.
    if target.exists():
        target.with_suffix(target.suffix + ".bak").write_text(
            target.read_text(encoding="utf-8"), encoding="utf-8"
        )
    target.write_text(text, encoding="utf-8")
    return []


def _load_bpmn_for(path: str | Path) -> str:
    """Load the workflow IR at ``path`` and serialize it to BPMN for the editor
    (migrating an older v1 file to v2 on the way, so any workflow is editable)."""
    from .migrate import migrate_to_current

    doc = migrate_to_current(load_workflow(path))
    return ir_to_bpmn(doc)


def edit_workflow(
    path: str | Path,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    open_browser: bool = True,
    serve_forever: bool = True,
):
    """Launch the ephemeral visual editor for the workflow IR at ``path``.

    Starts a LOOPBACK-ONLY HTTP server serving the bpmn-js host (IR->BPMN), opens the
    browser, and round-trips each Save back to the IR file. Because the server can WRITE
    the workflow file, the write endpoint is guarded TWO ways against a malicious page in
    the same browser: it binds to ``127.0.0.1`` only AND ``/save`` requires a
    per-session token (embedded in the served page, which a cross-origin site cannot
    read) plus a loopback ``Host`` header (DNS-rebinding defense). Returns
    ``(server, host, port, token)``; with ``serve_forever`` it blocks until interrupted
    (Ctrl-C, clean shutdown). Set ``serve_forever=False`` to start it on a background
    thread and return the handle (used by tests)."""
    import http.server
    import secrets
    import threading
    import webbrowser

    target = Path(path)
    bpmn_holder = {"xml": _load_bpmn_for(target)}
    token = secrets.token_urlsafe(18)  # per-session secret guarding the write endpoint

    class _Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def _host_ok(self) -> bool:
            # DNS-rebinding defense: the Host header must be the loopback we bound to.
            h = (self.headers.get("Host") or "").split(":")[0]
            return h in ("127.0.0.1", "localhost", "[::1]", "")

        def _send(self, code, body, ctype="text/html; charset=utf-8"):
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # noqa: N802 - stdlib handler name
            if not self._host_ok():
                self._send(403, "forbidden", "text/plain")
                return
            if self.path in ("/", "/index.html"):
                self._send(200, build_host_html(bpmn_holder["xml"], token=token))
            elif self.path == "/workflow.bpmn":
                self._send(200, bpmn_holder["xml"], "application/xml")
            else:
                self._send(404, "not found", "text/plain")

        def do_POST(self):  # noqa: N802 - stdlib handler name
            if self.path != "/save":
                self._send(404, '{"errors":["unknown endpoint"]}', "application/json")
                return
            # CSRF / DNS-rebinding guard: require the per-session token (a cross-origin
            # page can't read the host HTML to learn it) AND a loopback Host.
            if not self._host_ok() or not secrets.compare_digest(
                self.headers.get("X-Rebar-Token", ""), token
            ):
                self._send(403, '{"errors":["forbidden (bad token/origin)"]}', "application/json")
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
            except ValueError:
                self._send(400, '{"errors":["bad Content-Length"]}', "application/json")
                return
            xml = self.rfile.read(length).decode("utf-8")
            errors = save_bpmn_to_ir(xml, target)
            if errors:
                self._send(422, json.dumps({"errors": errors}), "application/json")
            else:
                bpmn_holder["xml"] = _load_bpmn_for(target)  # re-baseline from the saved IR
                self._send(200, json.dumps({"ok": True}), "application/json")

    server = http.server.ThreadingHTTPServer((host, port), _Handler)
    bound_host, bound_port = server.server_address[0], server.server_address[1]
    url = f"http://{bound_host}:{bound_port}/"
    if open_browser:
        threading.Thread(target=lambda: webbrowser.open(url), daemon=True).start()
    if not serve_forever:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server, bound_host, bound_port, token
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
    return server, bound_host, bound_port, token
