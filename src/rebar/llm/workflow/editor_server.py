"""Loopback HTTP transport for the ephemeral bpmn-js visual editor (part C of 744b).

This module holds the server-side transport that
:func:`rebar.llm.workflow.editor.edit_workflow` assembles: the stdlib ``http.server``
request handler (:class:`_Handler`) plus the per-session state it operates on
(:class:`EditorSession`). The handler was hoisted out of ``editor.py`` — where it was a
nested closure inside ``edit_workflow`` — to MODULE level so the launcher shrinks to server
assembly and the handler is independently importable/testable.

The closure's captured state (the write target, the per-session token, the resolved
prompts/contracts, the repo root, the mutable in-memory BPMN buffer, and the editor DOMAIN
callbacks — host-page render, BPMN<->IR round-trip, config validation) is threaded EXPLICITLY
through :class:`EditorSession`, injected per-server via ``functools.partial(_Handler,
session=…)``. Keeping the domain functions in ``editor.py`` and INJECTING them (rather than
importing ``editor`` here) keeps this module free of a back-edge into ``editor`` — no import
cycle (``python scripts/check_import_cycles.py`` stays green).

Security properties are UNCHANGED from the pre-split closure: the server binds loopback-only
(in ``edit_workflow``), the write/sensitive endpoints require a loopback ``Host`` header AND
the per-session ``X-Rebar-Token`` (constant-time ``compare_digest``), and asset serving is
allow-listed by name (no path traversal — only the two known bundle files are ever served).
"""

from __future__ import annotations

import http.server
import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# These live in NON-editor modules, so importing them here creates no back-edge into
# ``editor`` (keeping this module out of the import cycle — see the module docstring).
from rebar.llm.prompting.prompt_library import create_prompt, enumerate_library

from .prompt_authoring import list_prompts, prompt_write_target, save_prompt

# The editor front-end (bpmn-js Modeler + properties panel + auto-layout) is BUILT from
# editor_assets/ to a single self-contained bundle, vendored here and served LOCALLY — no
# CDN and no runtime npm (it is edit-time, in-browser only; the Python side stays stdlib).
# Rebuild with `npm --prefix src/rebar/llm/workflow/editor_assets run build`.
_ASSETS_DIR = Path(__file__).parent / "editor_assets" / "dist"
_ASSET_CONTENT_TYPES = {
    "editor.js": "application/javascript; charset=utf-8",
    "editor.css": "text/css; charset=utf-8",
}
_PREVIEW_PATHS = ("/criterion/preview", "/criterion/preview/status")
_POST_WRITE_PATHS = ("/save", "/prompt/save", "/validate", "/library/create", *_PREVIEW_PATHS)


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


@dataclass
class EditorSession:
    """The per-edit-session state a :class:`_Handler` serves.

    Bundles the state the handler used to CAPTURE as a closure from ``edit_workflow``: the
    write ``target`` IR file, the per-session ``token`` guarding writes, the resolved
    ``prompts`` / ``contracts`` shown in the inspector, the ``repo_root``, the static
    ``overlay_triggers`` / ``kind_help`` seeds (precomputed once — they do not change over the
    session), the mutable in-memory ``bpmn_xml`` buffer (re-baselined after each Save), and
    the editor DOMAIN callbacks (``render_host_html`` / ``save_ir`` / ``reload_bpmn`` /
    ``validate_config``). The callbacks are injected from ``editor.py`` so this module never
    imports ``editor`` (no import cycle)."""

    target: Path
    token: str
    repo_root: Any
    prompts: dict[str, str]
    contracts: dict[str, dict[str, Any]]
    overlay_triggers: list[dict[str, Any]]
    kind_help: dict[str, Any]
    bpmn_xml: str
    render_host_html: Callable[..., str]
    save_ir: Callable[..., list[str]]
    reload_bpmn: Callable[..., str]
    validate_config: Callable[..., dict[str, Any]]


class _Handler(http.server.BaseHTTPRequestHandler):
    """The editor's stdlib HTTP request handler, bound to one :class:`EditorSession`.

    Instantiated per request by ``ThreadingHTTPServer``; the session is injected PER-SERVER
    via ``functools.partial(_Handler, session=…)`` and stored on ``self.session`` BEFORE
    ``super().__init__`` (the base handler processes the request inside its constructor, so
    the session must be in place first)."""

    def __init__(self, *args: Any, session: EditorSession, **kwargs: Any) -> None:
        self.session = session
        super().__init__(*args, **kwargs)

    def log_message(self, *a):  # quiet
        pass

    def _host_ok(self) -> bool:
        # DNS-rebinding defense: the Host header must be the loopback we bound to.
        # A missing/empty Host is rejected — HTTP/1.1 clients (browsers, urllib)
        # always send one; only HTTP/1.0 or crafted requests omit it, which this
        # guard should refuse rather than admit.
        h = (self.headers.get("Host") or "").split(":")[0]
        return h in ("127.0.0.1", "localhost", "[::1]")

    def _authed(self) -> bool:
        # The shared CSRF / DNS-rebinding guard for the WRITE-or-sensitive endpoints
        # (/save, /prompts, /prompt, /prompt/save, /validate): a loopback Host AND the
        # per-session token (a cross-origin page can't read the host HTML to learn it).
        return self._host_ok() and secrets.compare_digest(
            self.headers.get("X-Rebar-Token", ""), self.session.token
        )

    def _serve_prompt(self):
        # GET /prompt?id=<id> → {id, text, meta, target} (the resolved write target
        # shown BEFORE save, per AC). Token+Host guarded; 404/JSON for an unknown id.
        if not self._authed():
            self._send(403, '{"errors":["forbidden (bad token/origin)"]}', "application/json")
            return
        from urllib.parse import parse_qs, urlsplit

        pid = (parse_qs(urlsplit(self.path).query).get("id") or [""])[0]
        target = prompt_write_target(pid, repo_root=self.session.repo_root)
        try:
            from rebar.llm.prompting.prompts import get_prompt, parse_front_matter

            prompt = get_prompt(pid, repo_root=self.session.repo_root)
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
        from rebar.llm.prompting.prompts import _catalog_dir, _packaged_prompt_files

        if self.session.repo_root:
            user = Path(self.session.repo_root) / ".rebar" / "prompts" / f"{pid}.md"
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
                self.session.render_host_html(
                    self.session.bpmn_xml,
                    token=self.session.token,
                    prompts=self.session.prompts,
                    contracts=self.session.contracts,
                    # Re-enumerate the library on each host load so a just-authored
                    # criterion/prompt is offered on reload; the overlay-trigger seed is
                    # static for the session (the client maintains it as triggers are added).
                    library=enumerate_library(repo_root=self.session.repo_root),
                    overlay_triggers=self.session.overlay_triggers,
                ),
            )
        elif self.path == "/workflow.bpmn":
            self._send(200, self.session.bpmn_xml, "application/xml")
        elif self.path == "/prompts":
            # The prompt LIBRARY: built-in + project prompts (story 6592). Same
            # token+Host guard as /save (it reveals the repo's prompt inventory).
            if not self._authed():
                self._send(403, '{"errors":["forbidden (bad token/origin)"]}', "application/json")
                return
            self._send(200, json.dumps(list_prompts(self.session.repo_root)), "application/json")
        elif self.path == "/library":
            # The authorable prompt + criterion LIBRARY (story B-DM/B-UX): the editor's
            # criterion-prompt picker re-fetches this after authoring a new entry so the
            # new id is immediately selectable. Same token+Host guard as /prompts.
            if not self._authed():
                self._send(403, '{"errors":["forbidden (bad token/origin)"]}', "application/json")
                return
            self._send(
                200,
                json.dumps(enumerate_library(repo_root=self.session.repo_root)),
                "application/json",
            )
        elif self.path == "/help":
            # The per-kind help DATA (story 998e): element kinds + expected JSON
            # shape per kind. Host-guarded (read-only, no token needed — it is the
            # same static map already injected as window.REBAR_KIND_HELP).
            self._send(200, json.dumps(self.session.kind_help), "application/json")
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
        if self.path not in _POST_WRITE_PATHS:
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
        if self.path == "/library/create":
            self._library_create(payload)
            return
        if self.path in _PREVIEW_PATHS:
            # Spike-gate preview (sync-within-timeout else a 202 job + /status poll).
            from .criterion_preview import handle_preview_post

            code, body = handle_preview_post(
                self.path, payload, repo_root=str(self.session.repo_root)
            )
            self._send(code, json.dumps(body), "application/json")
            return
        xml = payload.decode("utf-8")
        errors = self.session.save_ir(xml, self.session.target)
        if errors:
            self._send(422, json.dumps({"errors": errors}), "application/json")
        else:
            # re-baseline from the saved IR
            self.session.bpmn_xml = self.session.reload_bpmn(self.session.target)
            self._send(200, json.dumps({"ok": True}), "application/json")

    def _save_prompt(self, raw: bytes):
        # POST /prompt/save body {id, meta, body, overwrite?} → save_prompt(...).
        # 200 {ok, path, kind} or 4xx {errors:[...]} for the non-happy paths (invalid
        # id / collision / neither-writable) — already token+Host guarded above.
        from .prompt_authoring import PromptWriteError

        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            self._send(400, json.dumps({"errors": [f"bad JSON body: {exc}"]}), "application/json")
            return
        try:
            result = save_prompt(
                data.get("id", ""),
                data.get("meta") or {},
                data.get("body", ""),
                repo_root=self.session.repo_root,
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

    def _library_create(self, raw: bytes):
        # POST /library/create body {id, title, description, body, kind} → author a NEW
        # prompt-library entry under config.repo_root()'s .rebar/prompts/<id>.md via
        # prompt_library.create_prompt (story B-UX). create_prompt always writes the user
        # OVERRIDE home (never the packaged dir / committed index), so the drift gate stays
        # green; it canonicalizes the front-matter and invalidates the registry caches so
        # the entry is immediately enumerable. 200 {ok:true, id, path} or 4xx {ok:false,
        # errors:[...]}. Token+Host guarded above.
        from rebar.llm.prompting.prompt_library import LibraryWriteError, PromptError
        from rebar.llm.prompting.prompts import write_front_matter

        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            self._send(
                400,
                json.dumps({"ok": False, "errors": [f"bad JSON body: {exc}"]}),
                "application/json",
            )
            return
        pid = str(data.get("id", "")).strip()
        kind = str(data.get("kind", "prompt"))
        meta: dict[str, Any] = {
            "title": str(data.get("title") or pid),
            # description is a REQUIRED front-matter key (create_prompt rejects an empty
            # one); fall back to the title/id so a minimal name+body form still validates.
            "description": str(data.get("description") or data.get("title") or pid),
        }
        body_md = str(data.get("body") or "")
        routing = data.get("routing")
        try:
            if kind == "criterion" and isinstance(routing, dict) and routing:
                # ACTIVATION flow: a project.<name> criterion authored end-to-end (rubric at the
                # sanitized criterion_prompt_id + its atomic routing overlay) so a net-new
                # project.<name> round-trips (stew-kid). Requires the project-prefix (6e31).
                from .criterion_preview import author_criterion

                path = author_criterion(str(self.session.repo_root), pid, meta, body_md, routing)
            elif kind == "criterion":
                # REFERENCE flow (bug jinx-node-mudra): a batch step references a criterion rubric
                # by its prompt-library id, so write the criterion-category rubric at the RAW id
                # (no criterion_prompt_id sanitization, no routing overlay) — the id the step
                # references IS the file that resolves. Forcing the overlay here 400'd an
                # un-namespaced id and stranded the reference on the step.
                from rebar.llm.prompting.prompt_library import CRITERION_CATEGORY

                meta["category"] = CRITERION_CATEGORY
                path = create_prompt(
                    pid, write_front_matter(meta, body_md), repo_root=self.session.repo_root
                )
            else:
                path = create_prompt(
                    pid, write_front_matter(meta, body_md), repo_root=self.session.repo_root
                )
        except (LibraryWriteError, PromptError) as exc:
            self._send(
                400,
                json.dumps({"ok": False, "id": pid, "errors": [str(exc)]}),
                "application/json",
            )
            return
        except Exception as exc:  # noqa: BLE001 — overlay write failed → 4xx (prompt left inactive)
            self._send(
                400,
                json.dumps({"ok": False, "id": pid, "errors": [f"overlay: {exc}"]}),
                "application/json",
            )
            return
        self._send(
            200,
            json.dumps({"ok": True, "id": pid, "path": str(path)}),
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
            result = self.session.validate_config(
                str(data.get("kind", "")),
                data.get("action"),
                data.get("config", "") or "",
                repo_root=self.session.repo_root,
            )
            self._send(200, json.dumps(result), "application/json")
        except Exception as exc:  # noqa: BLE001 - endpoint crash → 500 + unavailable
            self._send(
                500,
                json.dumps(
                    {
                        "ok": False,
                        "unavailable": True,
                        "errors": [{"path": "", "message": f"validation endpoint error: {exc}"}],
                    }
                ),
                "application/json",
            )
