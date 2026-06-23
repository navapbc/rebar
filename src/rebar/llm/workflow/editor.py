"""Ephemeral bpmn-js visual editing (744b) — view + edit a workflow IR visually.

Delivers the first-order requirement: a human opens a workflow's IR in a CONSTRAINED
visual editor (bpmn-js), edits the flow/steps/loops/conditionals, and saves back to
the IR — WITHOUT committing any visual artifact. The pieces:

  * **editor host** (:func:`build_host_html`) — a thin shell page that loads the vendored
    front-end bundle (built from ``editor_assets/``: a bpmn-js *Modeler* whose palette is
    BPMN-only, so edits stay in a metamodel that maps back to the IR; a properties panel
    for viewing/editing each step's rebar config; and bpmn-auto-layout so the diagram opens
    readable) and hands it the BPMN to edit, the ``rebar`` moddle descriptor (so extension
    elements survive), and the per-session token. On Save the bundle POSTs the BPMN back.
  * **edit-time launcher** (:func:`edit_workflow`) — loads the workflow IR, serializes
    it to BPMN (the 00da serializer), starts a LOCAL, loopback-only HTTP server
    (stdlib ``http.server`` — no new runtime dependency), serves the bundle, opens the
    browser, and on Save round-trips BPMN->IR and writes ONLY the IR file (the BPMN is
    ephemeral, held in memory, never written to git). An invalid edit (a shape the IR
    can't express) is rejected with located errors and the file is left untouched.

Edit-time only + out-of-process: the editor runs in the BROWSER from a LOCALLY-served,
vendored bundle (no CDN, no runtime npm); the Python side is stdlib + the existing
serializer, so the runtime/client surface is unaffected. The round-trip logic is factored
into pure functions so it is testable without a browser; the human validates the visual
interaction (and a faithful Node E2E tier in ``tests/e2e`` exercises the real bpmn-io
serialization/layout the bundle uses).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .bpmn import REBAR_MODDLE_DESCRIPTOR, bpmn_to_ir, ir_to_bpmn
from .lint import lint_workflow
from .schema import dump_workflow, load_workflow

# The editor front-end (bpmn-js Modeler + properties panel + auto-layout) is BUILT from
# editor_assets/ to a single self-contained bundle, vendored here and served LOCALLY — no
# CDN and no runtime npm (it is edit-time, in-browser only; the Python side stays stdlib).
# Rebuild with `npm --prefix src/rebar/llm/workflow/editor_assets run build`.
_ASSETS_DIR = Path(__file__).parent / "editor_assets" / "dist"
_ASSET_CONTENT_TYPES = {
    "editor.js": "application/javascript; charset=utf-8",
    "editor.css": "text/css; charset=utf-8",
}


def assets_available() -> bool:
    """Whether the built editor bundle is present (it ships in the wheel; absent only in a
    source checkout that has not run the editor_assets build)."""
    return all((_ASSETS_DIR / name).is_file() for name in _ASSET_CONTENT_TYPES)


def read_asset(name: str) -> bytes | None:
    """Bytes of a served editor asset (``editor.js`` / ``editor.css``), or None if the name
    is not an allow-listed asset or the file is missing. Name is allow-listed (no path
    traversal): only the two known bundle files are ever served."""
    if name not in _ASSET_CONTENT_TYPES:
        return None
    path = _ASSETS_DIR / name
    return path.read_bytes() if path.is_file() else None


def _collect_prompt_ids(steps: list[Any]) -> set[str]:
    """Every agent step's ``prompt`` id in a workflow (recursing into control bodies)."""
    out: set[str] = set()
    for s in steps:
        if not isinstance(s, dict):
            continue
        if isinstance(s.get("prompt"), str):
            out.add(s["prompt"])
        for block, *keys in (("loop", "body"), ("map", "body"), ("branch", "then", "else")):
            blk = s.get(block)
            if isinstance(blk, dict):
                for key in keys:
                    if isinstance(blk.get(key), list):
                        out |= _collect_prompt_ids(blk[key])
    return out


def resolve_prompts(doc: dict[str, Any], path: str | Path) -> dict[str, str]:
    """Resolve each agent step's prompt id to its TEXT, so the editor can show what an
    agent step actually runs (the prompt text is otherwise invisible — it lives in a
    reviewer or ``.rebar/prompts/<id>.md`` file). Best-effort: an unresolved id maps to a
    short placeholder rather than failing the editor."""
    repo_root: Any
    try:
        from rebar import config

        repo_root = config.repo_root()
    except Exception:  # noqa: BLE001 - fall back to the file's own tree
        repo_root = Path(path).resolve().parent

    out: dict[str, str] = {}
    for pid in _collect_prompt_ids(doc.get("steps", []) or []):
        text = ""
        try:
            from rebar.llm.prompts import get_prompt

            text = get_prompt(pid, repo_root=repo_root).text
        except Exception:  # noqa: BLE001 - try a user prompt file, then give up gracefully
            try:
                from rebar.llm.prompts import parse_front_matter

                pf = Path(repo_root) / ".rebar" / "prompts" / f"{pid}.md"
                if pf.is_file():
                    _meta, text = parse_front_matter(pf.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                text = ""
        out[pid] = text or f"(prompt {pid!r} not found under .rebar/prompts/)"
    return out


# The defined EMPTY/no-contract inspector state (workflow authoring v2, 5e78 AC): a
# step with no declared contract — or nothing selected — renders this, never a crash.
_EMPTY_CONTRACT_VIEW: dict[str, Any] = {
    "has_contract": False,
    "description": "",
    "consumes": [],
    "produces": [],
}


def _schema_fields(schema_name: str | None) -> list[dict[str, Any]]:
    """The flat field list (``{name, type, required, description}``) of a contract
    schema's top-level object ``properties``, for the inspector's CONSUMES/PRODUCES.
    Best-effort: an unreadable/non-object schema yields an empty list (never raises)."""
    if not schema_name:
        return []
    try:
        from rebar import schemas

        schema = schemas.load(schema_name)
    except Exception:  # noqa: BLE001 - an unresolvable contract surfaces as empty, not a crash
        return []
    props = schema.get("properties")
    if not isinstance(props, dict):
        return []
    required = set(schema.get("required") or [])
    fields: list[dict[str, Any]] = []
    for name, sub in props.items():
        sub = sub if isinstance(sub, dict) else {}
        typ = sub.get("type")
        if typ is None and "$ref" in sub:
            typ = "object"
        if isinstance(typ, list):
            typ = " | ".join(str(t) for t in typ)
        fields.append(
            {
                "name": name,
                "type": typ or "",
                "required": name in required,
                "description": sub.get("description", ""),
            }
        )
    return fields


def step_contract_view(uses: str | None) -> dict[str, Any]:
    """The editor inspector's read-only view of a scripted op's CONTRACT: its
    description plus CONSUMES (input fields) and PRODUCES (output fields). An op with
    no declared contract (or ``None``/unknown) yields the defined empty state, so the
    inspector always renders something (workflow authoring v2, 5e78)."""
    if not uses:
        return dict(_EMPTY_CONTRACT_VIEW)
    try:
        from .executor import contract_for

        contract = contract_for(uses)
    except Exception:  # noqa: BLE001 - registry trouble degrades to the empty state
        contract = None
    if contract is None:
        return dict(_EMPTY_CONTRACT_VIEW)
    return {
        "has_contract": True,
        "description": contract.description,
        "consumes": _schema_fields(contract.input_schema),
        "produces": _schema_fields(contract.output_schema),
    }


def prompt_contract_view(prompt_id: str | None, *, repo_root: Any = None) -> dict[str, Any]:
    """The inspector's read-only contract view of an AGENT step's prompt (story 4b2f):
    its ``description`` plus CONSUMES (``inputs``) / PRODUCES (``outputs``), built from
    the prompt front-matter. A prompt's ``inputs``/``outputs`` may be schema NAMES
    (resolved via ``rebar.schemas`` like the scripted path) or may be absent → the
    empty/no-contract state. Best-effort: an unresolvable/unknown prompt id degrades to
    the empty state, never raises."""
    if not prompt_id:
        return dict(_EMPTY_CONTRACT_VIEW)
    try:
        from rebar.llm.prompts import get_prompt

        prompt = get_prompt(prompt_id, repo_root=repo_root)
    except Exception:  # noqa: BLE001 - an unknown/malformed prompt surfaces as empty, not a crash
        return dict(_EMPTY_CONTRACT_VIEW)
    consumes = _schema_fields(prompt.inputs) if isinstance(prompt.inputs, str) else []
    produces = _schema_fields(prompt.outputs) if isinstance(prompt.outputs, str) else []
    if not (prompt.description or consumes or produces):
        return dict(_EMPTY_CONTRACT_VIEW)
    return {
        "has_contract": True,
        "description": prompt.description or "",
        "consumes": consumes,
        "produces": produces,
    }


def resolve_contracts(doc: dict[str, Any], *, repo_root: Any = None) -> dict[str, dict[str, Any]]:
    """Map each step's contract-bearing key to its read-only contract view, so the
    editor can surface a selected node's contract (keyed by the element ``name`` the
    bundle looks up): a SCRIPTED step's ``uses`` op name → :func:`step_contract_view`,
    and an AGENT step's ``prompt`` id → :func:`prompt_contract_view`."""
    out: dict[str, dict[str, Any]] = {}
    for s in doc.get("steps", []) or []:
        out.update(_contracts_in(s, repo_root=repo_root))
    return out


def _contracts_in(step: Any, *, repo_root: Any = None) -> dict[str, dict[str, Any]]:
    """Recurse a step (and any nested branch/loop/map frames) collecting the contract
    view of every scripted ``uses`` op AND every agent ``prompt`` id encountered."""
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(step, dict):
        return out
    uses = step.get("uses")
    if isinstance(uses, str) and uses and uses not in out:
        out[uses] = step_contract_view(uses)
    prompt_id = step.get("prompt")
    if isinstance(prompt_id, str) and prompt_id and prompt_id not in out:
        out[prompt_id] = prompt_contract_view(prompt_id, repo_root=repo_root)
    for block, *keys in (("loop", "body"), ("map", "body"), ("branch", "then", "else")):
        blk = step.get(block)
        if isinstance(blk, dict):
            for key in keys:
                for child in blk.get(key) or []:
                    out.update(_contracts_in(child, repo_root=repo_root))
    return out


def build_host_html(
    bpmn_xml: str,
    *,
    token: str = "",
    descriptor: dict[str, Any] | None = None,
    prompts: dict[str, str] | None = None,
    contracts: dict[str, dict[str, Any]] | None = None,
) -> str:
    """The editor host page: a thin shell that loads the vendored bundle (``/assets/…``)
    and hands it three globals — the BPMN to edit, the ``rebar`` moddle descriptor (so
    extension elements survive), and the per-session token. All editor logic (the Modeler,
    the properties panel, auto-layout, and the Save POST to ``/save``) lives in the bundle.
    The token is embedded here and sent by the bundle on Save; a cross-origin page cannot
    READ this page (so cannot learn the token), closing the CSRF/DNS-rebinding write vector
    on ``/save``. The Modeler's palette is BPMN-only, so a human cannot draw a shape that
    does not map back to the IR."""
    desc = json.dumps(descriptor or REBAR_MODDLE_DESCRIPTOR)
    diagram = json.dumps(bpmn_xml)
    tok = json.dumps(token)
    prompt_map = json.dumps(prompts or {})
    contract_map = json.dumps(contracts or {})
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <title>rebar — visual workflow editor</title>
  <link rel="stylesheet" href="/assets/editor.css"/>
  <style>
    html, body {{ height: 100%; margin: 0; font-family: system-ui, sans-serif; }}
    #bar {{ height: 44px; display: flex; align-items: center; gap: 12px; padding: 0 12px;
            background: #1f2430; color: #e6e6e6; box-sizing: border-box; }}
    #bar button {{ padding: 6px 14px; cursor: pointer; }}
    #status {{ margin-left: auto; font-size: 13px; }}
    .ok {{ color: #7ee787; }} .err {{ color: #ff7b72; }}
    #main {{ display: flex; height: calc(100% - 44px); }}
    #canvas {{ flex: 1 1 auto; height: 100%; }}
    #properties {{ flex: 0 0 320px; height: 100%; overflow: auto;
                   border-left: 1px solid #ccc; background: #fafafa; }}
  </style>
</head>
<body>
  <div id="bar">
    <strong>rebar</strong> visual workflow editor
    <button id="save">Save to IR</button>
    <span id="status">loading…</span>
  </div>
  <div id="main">
    <div id="canvas"></div>
    <div id="properties"></div>
  </div>
  <script>
    window.REBAR_MODDLE = {desc};
    window.REBAR_DIAGRAM = {diagram};
    window.REBAR_TOKEN = {tok};
    window.REBAR_PROMPTS = {prompt_map};
    window.REBAR_CONTRACTS = {contract_map};
  </script>
  <script src="/assets/editor.js"></script>
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

    if not assets_available():
        from rebar.llm.errors import WorkflowError

        raise WorkflowError(
            "the editor front-end bundle is missing "
            f"({_ASSETS_DIR}/editor.js). Build it with: "
            "npm --prefix src/rebar/llm/workflow/editor_assets install && "
            "npm --prefix src/rebar/llm/workflow/editor_assets run build"
        )

    target = Path(path)
    from .migrate import migrate_to_current

    doc = migrate_to_current(load_workflow(target))
    bpmn_holder = {"xml": ir_to_bpmn(doc)}
    prompts = resolve_prompts(doc, target)  # prompt id -> text, for the editor to display
    try:
        from rebar import config as _cfg

        _repo_root: Any = _cfg.repo_root()
    except Exception:  # noqa: BLE001 - fall back to the file's own tree
        _repo_root = Path(target).resolve().parent
    contracts = resolve_contracts(doc, repo_root=_repo_root)  # op/prompt -> view, for inspector
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
                self._send(
                    200,
                    build_host_html(
                        bpmn_holder["xml"], token=token, prompts=prompts, contracts=contracts
                    ),
                )
            elif self.path == "/workflow.bpmn":
                self._send(200, bpmn_holder["xml"], "application/xml")
            elif self.path.startswith("/assets/"):
                name = self.path[len("/assets/") :]
                data = read_asset(name)
                if data is None:
                    self._send(404, "not found", "text/plain")
                else:
                    self._send(200, data, _ASSET_CONTENT_TYPES[name])
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
