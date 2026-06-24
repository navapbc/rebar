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

# Inspector contract-views live in their own module; re-exported (explicit `as` form)
# so `editor.step_contract_view` / `resolve_contracts` stay the stable surface (748a).
from .editor_contracts import prompt_contract_view as prompt_contract_view
from .editor_contracts import resolve_contracts as resolve_contracts
from .editor_contracts import step_contract_view as step_contract_view
from .lint import lint_workflow
from .prompt_authoring import list_prompts, prompt_write_target, save_prompt
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


# ── The defined /validate response shape (story 998e) ───────────────────────────
# Every path through validate_node_config returns EXACTLY this shape so the bundle
# can map it uniformly: {"ok": bool, "errors": [{"path": str, "message": str}],
# "unavailable": bool}. The three failure axes are kept DISTINCT:
#   * empty config           → ok:True,  errors:[],   unavailable:False (valid)
#   * malformed JSON         → ok:False, errors:[…],  unavailable:False (user-fixable)
#   * schema violation       → ok:False, errors:[…],  unavailable:False (user-fixable)
#   * validator itself errored → ok:False, errors:[…], unavailable:True (dead validator)
# `unavailable:True` is NEVER a false `ok:True` and NEVER a silent swallow — it is the
# explicit "validation unavailable" state the front-end renders as a distinct banner.


def _config_input_schema(kind: str, action: str | None, repo_root: Any) -> str | None:
    """The INPUT-contract schema NAME a node's ``with`` must satisfy — delegates to the
    shared resolver so edit-time validation matches the runtime net exactly (b642)."""
    from .executor import input_schema_for

    return input_schema_for(kind, action, repo_root)


def validate_node_config(
    kind: str, action: str | None, config_text: str, repo_root: Any = None
) -> dict[str, Any]:
    """Validate one editor node's raw JSON config against its INPUT contract,
    returning the DEFINED shape ``{"ok", "errors": [{"path","message"}], "unavailable"}``.

    The decision table (each axis kept DISTINCT — see the module note above):

    * EMPTY (empty/whitespace ``config_text``) → ``ok:True`` (an empty config is valid,
      not malformed).
    * MALFORMED JSON (unparseable) → ``ok:False`` + a single ``invalid JSON: <detail>``
      error, ``unavailable:False`` (a real, user-fixable error — NOT unavailable).
    * PARSEABLE → validate ``config["with"]`` against the node's input contract:
      - no contract → ``ok:True`` (nothing to validate).
      - schema VIOLATION → ``ok:False`` + one ``{path, message}`` per ``ValidationError``,
        ``unavailable:False``.
      - validator FAILURE (the validator itself errors — unresolvable ``$ref`` / unknown
        schema / any non-``ValidationError``) → ``ok:False`` + a single
        ``validation unavailable: <detail>`` error AND ``unavailable:True``. NEVER a
        false ``ok:True``, never a silent swallow."""
    if not config_text or not config_text.strip():
        return {"ok": True, "errors": [], "unavailable": False}
    try:
        config = json.loads(config_text)
    except (ValueError, UnicodeDecodeError) as exc:
        return {
            "ok": False,
            "errors": [{"path": "", "message": f"invalid JSON: {exc}"}],
            "unavailable": False,
        }
    schema_name = _config_input_schema(kind, action, repo_root)
    if not schema_name:
        return {"ok": True, "errors": [], "unavailable": False}
    with_value = config.get("with", {}) if isinstance(config, dict) else config

    from rebar import schemas

    try:
        validator = schemas.validator(schema_name)
        errors = sorted(validator.iter_errors(with_value), key=lambda e: list(e.absolute_path))
    except Exception as exc:  # noqa: BLE001 - validator itself errored: fail-loud, DISTINCT
        return {
            "ok": False,
            "errors": [{"path": "", "message": f"validation unavailable: {exc}"}],
            "unavailable": True,
        }
    if not errors:
        return {"ok": True, "errors": [], "unavailable": False}
    out: list[dict[str, str]] = []
    for err in errors:
        try:
            path = err.json_path
        except Exception:  # noqa: BLE001 - older jsonschema without json_path
            path = "/".join(str(p) for p in err.absolute_path)
        out.append({"path": path, "message": err.message})
    return {"ok": False, "errors": out, "unavailable": False}


# ── Per-kind HELP data (story 998e) ─────────────────────────────────────────────
# The single source of truth for the editor's help panel: the 5 element kinds, each
# with the JSON shape its config carries. Surfaced to the bundle as a window global
# (and via GET /help) so the help text is testable and never drifts from Python.
_NODE_KIND_HELP: dict[str, dict[str, Any]] = {
    "scripted": {
        "title": "Scripted step",
        "summary": "Runs a registered Python op (deterministic). Its `with` is "
        "validated against the op's input contract.",
        "action_key": "uses",
        "shape": {"uses": "<op-id>", "with": {"<input>": "<value>"}},
    },
    "agent": {
        "title": "Agent step",
        "summary": "Runs a prompt against the LLM. Its `with` is validated against "
        "the prompt's declared inputs contract.",
        "action_key": "prompt",
        "shape": {"prompt": "<prompt-id>", "with": {"<input>": "<value>"}},
    },
    "branch": {
        "title": "Branch (conditional)",
        "summary": "Chooses between `then` / `else` bodies on a condition.",
        "action_key": None,
        "shape": {"branch": {"when": "<expr>", "then": ["<step>"], "else": ["<step>"]}},
    },
    "loop": {
        "title": "Loop",
        "summary": "Repeats a `body` while a condition holds (bounded by max iterations).",
        "action_key": None,
        "shape": {"loop": {"while": "<expr>", "max": 10, "body": ["<step>"]}},
    },
    "map": {
        "title": "Map (fan-out)",
        "summary": "Runs a `body` once per item of an input collection.",
        "action_key": None,
        "shape": {"map": {"over": "<expr>", "as": "<var>", "body": ["<step>"]}},
    },
}


def node_kind_help() -> dict[str, dict[str, Any]]:
    """The per-kind help DATA for the editor's help panel: the 5 element kinds
    (scripted/agent/branch/loop/map), each with a title, a one-line summary, and the
    expected JSON ``shape`` of its config. The single Python source of truth the
    bundle renders (window global + ``GET /help``) so the help never drifts from the
    contracts. Returns a deep copy so callers cannot mutate the canonical map."""
    return json.loads(json.dumps(_NODE_KIND_HELP))


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
    help_map = json.dumps(node_kind_help())
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
    #rebar-library {{ flex: 0 0 300px; height: 100%; overflow: auto; padding: 8px;
                      border-left: 1px solid #ccc; background: #f4f6fa;
                      box-sizing: border-box; font-size: 13px; }}
    #rebar-library h3 {{ margin: 8px 0 4px; font-size: 13px; }}
    #rebar-library select, #rebar-library input, #rebar-library textarea, #rebar-library button
      {{ display: block; width: 100%; margin: 3px 0; box-sizing: border-box; }}
    #rebar-library label {{ display: block; margin: 4px 0; }}
    #rebar-library .rebar-lib-target {{ font-family: monospace; font-size: 11px; color: #555;
                                        margin-top: 6px; word-break: break-all; }}
    #rebar-library .ok {{ color: #1a7f37; }} #rebar-library .err {{ color: #cf222e; }}
    #rebar-validate-region {{ white-space: pre-wrap; font-size: 12px; padding: 8px;
                              margin: 0 0 6px; border-radius: 4px; box-sizing: border-box; }}
    .rebar-validate-errors {{ background: #ffeef0; color: #cf222e;
                              border: 1px solid #cf222e; }}
    .rebar-validate-unavailable {{ background: #fff8c5; color: #7d4e00;
                                   border: 1px solid #d4a72c; }}
    #rebar-kind-help h3 {{ margin: 12px 0 4px; }}
    #rebar-kind-help .rebar-help-kind {{ margin: 0 0 10px; }}
    #rebar-kind-help .rebar-help-summary {{ color: #555; margin: 2px 0; }}
    #rebar-kind-help .rebar-help-shape {{ background: #eef1f6; padding: 6px; margin: 3px 0;
                                          font-size: 11px; overflow: auto; }}
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
    <div id="rebar-library"></div>
  </div>
  <script>
    window.REBAR_MODDLE = {desc};
    window.REBAR_DIAGRAM = {diagram};
    window.REBAR_TOKEN = {tok};
    window.REBAR_PROMPTS = {prompt_map};
    window.REBAR_CONTRACTS = {contract_map};
    window.REBAR_KIND_HELP = {help_map};
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

        def _authed(self) -> bool:
            # The shared CSRF / DNS-rebinding guard for the WRITE-or-sensitive endpoints
            # (/save, /prompts, /prompt, /prompt/save, /validate): a loopback Host AND the
            # per-session token (a cross-origin page can't read the host HTML to learn it).
            return self._host_ok() and secrets.compare_digest(
                self.headers.get("X-Rebar-Token", ""), token
            )

        def _serve_prompt(self):
            # GET /prompt?id=<id> → {id, text, meta, target} (the resolved write target
            # shown BEFORE save, per AC). Token+Host guarded; 404/JSON for an unknown id.
            if not self._authed():
                self._send(403, '{"errors":["forbidden (bad token/origin)"]}', "application/json")
                return
            from urllib.parse import parse_qs, urlsplit

            pid = (parse_qs(urlsplit(self.path).query).get("id") or [""])[0]
            target = prompt_write_target(pid, repo_root=_repo_root)
            try:
                from rebar.llm.prompts import get_prompt, parse_front_matter

                prompt = get_prompt(pid, repo_root=_repo_root)
                meta, _body = parse_front_matter(self._raw_prompt_text(pid) or "")
                payload = {"id": pid, "text": prompt.text, "meta": meta, "target": target}
                self._send(200, json.dumps(payload), "application/json")
            except Exception as exc:  # noqa: BLE001 - unknown/malformed id is a clean 404
                self._send(
                    404,
                    json.dumps({"errors": [f"unknown prompt {pid!r}: {exc}"], "target": target}),
                    "application/json",
                )

        def _raw_prompt_text(self, pid: str) -> str | None:
            # The RAW prompt file text (front-matter intact) so the edit form gets the
            # current front-matter; a project override wins over the packaged copy.
            from rebar.llm.prompts import _catalog_dir, _packaged_prompt_files

            if _repo_root:
                user = Path(_repo_root) / ".rebar" / "prompts" / f"{pid}.md"
                if user.is_file():
                    return user.read_text(encoding="utf-8")
            packaged = _packaged_prompt_files()
            if pid in packaged:
                return Path(str(_catalog_dir())).joinpath(packaged[pid]).read_text(encoding="utf-8")
            return None

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
            elif self.path == "/prompts":
                # The prompt LIBRARY: built-in + project prompts (story 6592). Same
                # token+Host guard as /save (it reveals the repo's prompt inventory).
                if not self._authed():
                    self._send(
                        403, '{"errors":["forbidden (bad token/origin)"]}', "application/json"
                    )
                    return
                self._send(200, json.dumps(list_prompts(_repo_root)), "application/json")
            elif self.path == "/help":
                # The per-kind help DATA (story 998e): element kinds + expected JSON
                # shape per kind. Host-guarded (read-only, no token needed — it is the
                # same static map already injected as window.REBAR_KIND_HELP).
                self._send(200, json.dumps(node_kind_help()), "application/json")
            elif self.path.startswith("/prompt?"):
                self._serve_prompt()
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
            if self.path not in ("/save", "/prompt/save", "/validate"):
                self._send(404, '{"errors":["unknown endpoint"]}', "application/json")
                return
            # CSRF / DNS-rebinding guard: require the per-session token (a cross-origin
            # page can't read the host HTML to learn it) AND a loopback Host.
            if not self._authed():
                self._send(403, '{"errors":["forbidden (bad token/origin)"]}', "application/json")
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
            except ValueError:
                self._send(400, '{"errors":["bad Content-Length"]}', "application/json")
                return
            payload = self.rfile.read(length)
            if self.path == "/prompt/save":
                self._save_prompt(payload)
                return
            if self.path == "/validate":
                self._validate(payload)
                return
            xml = payload.decode("utf-8")
            errors = save_bpmn_to_ir(xml, target)
            if errors:
                self._send(422, json.dumps({"errors": errors}), "application/json")
            else:
                bpmn_holder["xml"] = _load_bpmn_for(target)  # re-baseline from the saved IR
                self._send(200, json.dumps({"ok": True}), "application/json")

        def _save_prompt(self, raw: bytes):
            # POST /prompt/save body {id, meta, body, overwrite?} → save_prompt(...).
            # 200 {ok, path, kind} or 4xx {errors:[...]} for the non-happy paths (invalid
            # id / collision / neither-writable) — already token+Host guarded above.
            from .prompt_authoring import PromptWriteError

            try:
                data = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                self._send(
                    400, json.dumps({"errors": [f"bad JSON body: {exc}"]}), "application/json"
                )
                return
            try:
                result = save_prompt(
                    data.get("id", ""),
                    data.get("meta") or {},
                    data.get("body", ""),
                    repo_root=_repo_root,
                    overwrite=bool(data.get("overwrite")),
                )
            except PromptWriteError as exc:
                self._send(400, json.dumps({"errors": [str(exc)]}), "application/json")
                return
            except Exception as exc:  # noqa: BLE001 - any other write failure → clear 4xx
                self._send(
                    400, json.dumps({"errors": [f"prompt save failed: {exc}"]}), "application/json"
                )
                return
            self._send(
                200,
                json.dumps({"ok": True, "path": result["path"], "kind": result["kind"]}),
                "application/json",
            )

        def _validate(self, raw: bytes):
            # POST /validate body {kind, action, config} → validate_node_config(...) as
            # JSON 200 (the defined shape; even ok:false is HTTP 200 — a normal
            # validation result). If the endpoint handler ITSELF crashes unexpectedly,
            # return HTTP 500 with the same shape + unavailable:true so the client can
            # map 500 → the "validation unavailable" state too (story 998e).
            try:
                data = json.loads(raw.decode("utf-8"))
                result = validate_node_config(
                    str(data.get("kind", "")),
                    data.get("action"),
                    data.get("config", "") or "",
                    repo_root=_repo_root,
                )
                self._send(200, json.dumps(result), "application/json")
            except Exception as exc:  # noqa: BLE001 - endpoint crash → 500 + unavailable
                self._send(
                    500,
                    json.dumps(
                        {
                            "ok": False,
                            "unavailable": True,
                            "errors": [
                                {"path": "", "message": f"validation endpoint error: {exc}"}
                            ],
                        }
                    ),
                    "application/json",
                )

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
