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
from typing import Any, cast

from .bpmn import REBAR_MODDLE_DESCRIPTOR, bpmn_to_ir, ir_to_bpmn

# Inspector contract-views live in their own module; re-exported (explicit `as` form)
# so `editor.step_contract_view` / `resolve_contracts` stay the stable surface (748a).
from .editor_contracts import prompt_contract_view as prompt_contract_view
from .editor_contracts import resolve_contracts as resolve_contracts
from .editor_contracts import step_contract_view as step_contract_view

# The HTTP transport (the loopback request handler + its per-session state) lives in the
# sibling module so this file stays the DOMAIN + launcher surface (744b part C). editor.py
# imports the handler ONE-WAY; editor_server never imports editor back (no import cycle).
# The asset-serving + route constants are defined there and RE-EXPORTED here (the `as` form)
# so `editor.assets_available` / `editor.read_asset` / `editor._POST_WRITE_PATHS` /
# `editor._PREVIEW_PATHS` remain the stable surface tests and the launcher use.
from .editor_server import _ASSETS_DIR, EditorSession, _Handler, assets_available
from .editor_server import _POST_WRITE_PATHS as _POST_WRITE_PATHS
from .editor_server import _PREVIEW_PATHS as _PREVIEW_PATHS
from .editor_server import read_asset as read_asset
from .lint import lint_workflow
from .schema import dump_workflow, load_workflow


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


def _collect_overlay_triggers(steps: list[Any]) -> list[dict[str, Any]]:
    """Every overlay-trigger OUTPUT a workflow's ``overlay_triggers`` steps emit, as a flat
    list of ``{stepId, name, expr, label}`` (recursing into control bodies).

    Each ``overlay_triggers`` step yields one boolean output per ``with.keyword_triggers``
    name, plus ``has_children`` when ``structural`` is set, plus ``has_linked_<type>`` per
    ``with.linked_types`` entry. ``expr`` is the full ``${{ steps.<id>.outputs.<name> }}``
    reference a batch criterion's ``when`` overlay predicate stores — so the editor's
    ``when`` picker offers these as ready-to-use options (story B-UX)."""
    out: list[dict[str, Any]] = []
    for s in steps:
        if not isinstance(s, dict):
            continue
        if s.get("uses") == "overlay_triggers":
            step_id = str(s.get("id") or "")
            _with = s.get("with")
            with_ = _with if isinstance(_with, dict) else {}
            kw = with_.get("keyword_triggers")
            names: list[str] = list(kw.keys()) if isinstance(kw, dict) else []
            if with_.get("structural"):
                names.append("has_children")
            for t in with_.get("linked_types") or []:
                names.append(f"has_linked_{t}")
            for name in names:
                out.append(
                    {
                        "stepId": step_id,
                        "name": str(name),
                        "expr": f"${{{{ steps.{step_id}.outputs.{name} }}}}",
                        "label": f"{step_id}.{name}",
                    }
                )
        for block, *keys in (("loop", "body"), ("map", "body"), ("branch", "then", "else")):
            blk = s.get(block)
            if isinstance(blk, dict):
                for key in keys:
                    if isinstance(blk.get(key), list):
                        out.extend(_collect_overlay_triggers(blk[key]))
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
            except Exception:  # noqa: BLE001 — fail-open: user prompt-file read fails → empty text
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
    library: list[dict[str, Any]] | None = None,
    overlay_triggers: list[dict[str, Any]] | None = None,
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
    library_list = json.dumps(library or [])
    overlay_trigger_list = json.dumps(overlay_triggers or [])
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
    /* B-UX item 16: make every edit-panel field LABEL bold so it is visually separated
       from its help/description text (which run together otherwise). */
    #properties .bio-properties-panel-label {{ font-weight: 600; }}
    #rebar-library {{ flex: 0 0 300px; height: 100%; overflow: auto; padding: 8px;
                      border-left: 1px solid #ccc; background: #f4f6fa;
                      box-sizing: border-box; font-size: 13px; }}
    #rebar-library h3 {{ margin: 8px 0 4px; font-size: 13px; }}
    #rebar-library select, #rebar-library input, #rebar-library textarea, #rebar-library button
      {{ display: block; width: 100%; margin: 3px 0; box-sizing: border-box; }}
    #rebar-library label {{ display: block; margin: 4px 0; }}
    /* B-UX item 12: the "overwrite if exists" checkbox was full-width + block (stacked above
       its text, misaligned). Lay the row out as a flex line and let the checkbox size itself. */
    #rebar-library .rebar-overwrite-label {{ display: flex; align-items: center; gap: 6px; }}
    #rebar-library #rebar-prompt-overwrite {{ display: inline-block; width: auto; margin: 0;
                                              flex: 0 0 auto; }}
    /* B-UX item 13: inline id-collision notice under the id field. */
    #rebar-library .rebar-lib-idcheck {{ font-size: 11px; margin: 2px 0 4px; }}
    #rebar-library .rebar-lib-idcheck.err {{ color: #cf222e; }}
    #rebar-library .rebar-lib-idcheck.ok {{ color: #1a7f37; }}
    /* B-UX item 5: the on-demand insert panel revealed by the "Add step" button. */
    #rebar-library .rebar-insert-panel {{ padding: 4px 0; }}
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
    <button id="save">Save to Rebar</button>
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
    window.REBAR_LIBRARY = {library_list};
    window.REBAR_OVERLAY_TRIGGERS = {overlay_trigger_list};
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
    import functools
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
    prompts = resolve_prompts(doc, target)  # prompt id -> text, for the editor to display
    try:
        from rebar import config as _cfg

        _repo_root: Any = _cfg.repo_root()
    except Exception:  # noqa: BLE001 - fall back to the file's own tree
        _repo_root = Path(target).resolve().parent
    contracts = resolve_contracts(doc, repo_root=_repo_root)  # op/prompt -> view, for inspector
    token = secrets.token_urlsafe(18)  # per-session secret guarding the write endpoint

    # The handler operates on this EXPLICIT session (previously the closure it captured). The
    # DOMAIN callbacks (host-page render + BPMN<->IR round-trip + config validation) are
    # INJECTED so editor_server never imports editor back (no import cycle); the overlay-trigger
    # + help seeds are precomputed once (they are static for the session).
    session = EditorSession(
        target=target,
        token=token,
        repo_root=_repo_root,
        prompts=prompts,
        contracts=contracts,
        overlay_triggers=_collect_overlay_triggers(doc.get("steps", []) or []),
        kind_help=node_kind_help(),
        bpmn_xml=ir_to_bpmn(doc),
        render_host_html=build_host_html,
        save_ir=save_bpmn_to_ir,
        reload_bpmn=_load_bpmn_for,
        validate_config=validate_node_config,
    )

    handler = functools.partial(_Handler, session=session)
    server = http.server.ThreadingHTTPServer((host, port), handler)
    # server_address is typed as a broad union (str | bytes | tuple | …); for an AF_INET
    # HTTP server it is always (host: str, port: int), so narrow with a cast.
    bound_host = cast(str, server.server_address[0])
    bound_port = cast(int, server.server_address[1])
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
