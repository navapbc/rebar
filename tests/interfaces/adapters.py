"""Interface adapters: one common protocol, three implementations.

Each adapter drives the same ticket operations through a different rebar
interface — the Python library (in-process), the CLI (`python -m rebar.cli`
subprocess), and the MCP server (FastMCP tools) — and normalizes results to
plain Python values so test_parity.py can assert identical behavior without
per-interface branching.

All adapters target the repo named by the REBAR_ROOT env var (set by the
rebar_repo fixture), so the three operate on the SAME git-backed store.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, ClassVar

# The one shared structured-rejection code. A state-dependent write
# (transition/claim/reopen) that fails optimistic concurrency surfaces this
# identity across ALL three interfaces: the engine's exit code 10, which the
# library raises as ``rebar.ConcurrencyError`` and which the CLI returns as
# ``returncode == 10``. Any other failure (e.g. not-found, exit 1) is NOT this
# code, so parity tests can distinguish a concurrency rejection from any other
# error rather than collapsing every failure to a bare ``False``.
CONCURRENCY_CODE = 10


@dataclass(frozen=True)
class Outcome:
    """A structured result for a state-dependent write across interfaces.

    ``ok`` is True on success. On failure, ``code`` is the engine exit code that
    every interface agrees on (``CONCURRENCY_CODE`` for an optimistic-concurrency
    rejection, some other non-zero value otherwise) and ``error_type`` is the
    library exception class name (``"ConcurrencyError"`` for the shared identity)
    so a test can assert the rejection REASON, not just its presence.

    The object is truthy iff ``ok`` so existing ``assert adapter.transition(...)``
    truth checks keep working, while ``.is_concurrency`` exposes the shared code.
    """

    ok: bool
    code: int | None = None
    error_type: str | None = None

    def __bool__(self) -> bool:
        return self.ok

    @property
    def is_concurrency(self) -> bool:
        """True iff this is the ONE shared concurrency-rejection identity."""
        return (not self.ok) and self.code == CONCURRENCY_CODE

    @property
    def is_param_gap(self) -> bool:
        """True iff a contract param was requested that THIS transport does not expose.

        This is a DISTINCT outcome from a rule rejection: the surface never
        advertised the parameter (e.g. the MCP force/reason/caused_by/ref gaps),
        so the write op was not even attempted. The write-parity oracle maps this
        to its own ``PARAM_NOT_EXPOSED`` classification rather than collapsing it
        into a bare rejection, so closing the gap flips the classification and
        trips a strict-xfail.
        """
        return (not self.ok) and self.error_type == PARAM_NOT_EXPOSED_ERROR


# The typed identity for "this transport does not expose the requested param".
PARAM_NOT_EXPOSED_ERROR = "ParamNotExposed"

# Sentinels for the states parity tests assert on.
OK = Outcome(ok=True)
NOT_EXPOSED = Outcome(ok=False, error_type=PARAM_NOT_EXPOSED_ERROR)

# The reason-carrying contract params under test on the write ops (transition
# adds all five; claim exposes only ``force``). ``force_close`` is intentionally
# absent — blusterous-earthly-kitten retired it in favor of ``force``.
TRANSITION_WRITE_PARAMS = ("force", "reason", "close_class", "caused_by", "ref")


def _force_cli_args(force: str | None) -> list[str]:
    """Spell the unified ``--force[=<reason>]`` surface for the CLI.

    ``None`` means "not forcing" (no flag); ``""`` is a bare ``--force`` (the
    engine records ``"(no reason given)"``); any other string rides inline as
    ``--force=<reason>`` — byte-identically for ``claim`` and ``transition``.
    """
    if force is None:
        return []
    return ["--force"] if force == "" else [f"--force={force}"]


class Adapter:
    """Common interface protocol. Methods return normalized Python values.

    transition()/claim() return a structured :class:`Outcome` (truthy on success,
    falsy on rejection) carrying the shared rejection identity, so parity tests
    can assert both error-presence + store-invariance AND the rejection REASON
    (concurrency vs. anything else) uniformly across the three interfaces.
    """

    name: str

    def create(self, ticket_type: str, title: str, **kw: Any) -> str: ...
    def show(self, tid: str) -> dict: ...
    def list(self, **filters: Any) -> list[dict]: ...
    def transition(
        self,
        tid: str,
        current: str,
        target: str,
        *,
        force: str | None = None,
        reason: str | None = None,
        close_class: str | None = None,
        caused_by: str | None = None,
        ref: str | None = None,
    ) -> Outcome: ...
    def claim(
        self, tid: str, assignee: str | None = None, *, force: str | None = None
    ) -> Outcome: ...
    def comment(self, tid: str, body: str) -> None: ...
    def tag(self, tid: str, tag: str) -> None: ...
    def link(self, a: str, b: str, relation: str) -> None: ...
    def deps(self, tid: str) -> dict: ...
    def ready(self) -> Any: ...
    def next_batch(self, epic_id: str) -> dict: ...
    def search(self, query: str, **filters: Any) -> list[dict]: ...


# ── Library ────────────────────────────────────────────────────────────────
class LibraryAdapter(Adapter):
    name = "library"

    def __init__(self) -> None:
        import rebar

        self._r = rebar

    def create(self, ticket_type: str, title: str, **kw: Any) -> str:
        return self._r.create_ticket(ticket_type, title, **kw)

    def show(self, tid: str) -> dict:
        return self._r.show_ticket(tid)

    def list(self, **filters: Any) -> list[dict]:
        return self._r.list_tickets(**filters)

    def transition(
        self,
        tid: str,
        current: str,
        target: str,
        *,
        force: str | None = None,
        reason: str | None = None,
        close_class: str | None = None,
        caused_by: str | None = None,
        ref: str | None = None,
    ) -> Outcome:
        kw: dict[str, Any] = {}
        if force is not None:
            kw["force"] = force
        if reason is not None:
            kw["reason"] = reason
        if close_class is not None:
            kw["close_class"] = close_class
        if caused_by is not None:
            kw["caused_by"] = caused_by
        if ref is not None:
            kw["ref"] = ref
        try:
            self._r.transition(tid, current, target, **kw)
            return OK
        except self._r.RebarError as exc:
            return self._reject(exc)

    def claim(self, tid: str, assignee: str | None = None, *, force: str | None = None) -> Outcome:
        kw: dict[str, Any] = {}
        if force is not None:
            kw["force"] = force
        try:
            self._r.claim(tid, assignee=assignee, **kw)
            return OK
        except self._r.RebarError as exc:
            return self._reject(exc)

    def _reject(self, exc: Exception) -> Outcome:
        """Map a library RebarError to the shared structured Outcome.

        The exception's ``returncode`` (10 for ConcurrencyError) IS the shared
        engine code; its class name is the typed identity.
        """
        return Outcome(
            ok=False,
            code=getattr(exc, "returncode", None),
            error_type=type(exc).__name__,
        )

    def comment(self, tid: str, body: str) -> None:
        self._r.comment(tid, body)

    def tag(self, tid: str, tag: str) -> None:
        self._r.tag(tid, tag)

    def link(self, a: str, b: str, relation: str) -> None:
        self._r.link(a, b, relation)

    def deps(self, tid: str) -> dict:
        return self._r.deps(tid)

    def ready(self) -> Any:
        return self._r.ready()

    def next_batch(self, epic_id: str) -> dict:
        return self._r.next_batch(epic_id)

    def search(self, query: str, **filters: Any) -> list[dict]:
        return self._r.search(query, **filters)


# ── CLI ──────────────────────────────────────────────────────────────────────
class CliAdapter(Adapter):
    name = "cli"

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "rebar.cli", *args],
            capture_output=True,
            text=True,
        )

    def _ok_json(self, *args: str) -> Any:
        cp = self._run(*args)
        assert cp.returncode == 0, f"cli {args} failed: {cp.stderr}"
        return json.loads(cp.stdout)

    def create(self, ticket_type: str, title: str, **kw: Any) -> str:
        args = ["create", ticket_type, title]
        for key, val in kw.items():
            if val is None:
                continue
            flag = "--" + key
            if key == "tags" and isinstance(val, (list, tuple)):
                val = ",".join(val)
            args += [flag, str(val)]
        cp = self._run(*args, "--output", "json")
        assert cp.returncode == 0, f"cli create failed: {cp.stderr}"
        return str(json.loads(cp.stdout)["id"])

    def show(self, tid: str) -> dict:
        return self._ok_json("show", tid)

    # Library kwarg -> CLI list flag (ticket_type maps to --type).
    _LIST_FLAGS: ClassVar[dict] = {
        "status": "--status",
        "ticket_type": "--type",
        "priority": "--priority",
        "parent": "--parent",
        "has_tag": "--has-tag",
        "without_tag": "--without-tag",
        "include_archived": "--include-archived",
    }

    def list(self, **filters: Any) -> list[dict]:
        args = ["list"]
        for key, val in filters.items():
            if val is None or val is False:
                continue
            flag = self._LIST_FLAGS[key]
            if val is True:
                args.append(flag)
            else:
                args.append(f"{flag}={val}")
        return self._ok_json(*args)

    def transition(
        self,
        tid: str,
        current: str,
        target: str,
        *,
        force: str | None = None,
        reason: str | None = None,
        close_class: str | None = None,
        caused_by: str | None = None,
        ref: str | None = None,
    ) -> Outcome:
        args = ["transition", tid, current, target]
        if close_class is not None:
            args += ["--class", close_class]
        if reason is not None:
            args += ["--reason", reason]
        if caused_by is not None:
            args += ["--caused-by", caused_by]
        if ref is not None:
            args += ["--ref", ref]
        args += _force_cli_args(force)
        return self._outcome(self._run(*args))

    def claim(self, tid: str, assignee: str | None = None, *, force: str | None = None) -> Outcome:
        args = ["claim", tid]
        if assignee:
            args.append(f"--assignee={assignee}")
        args += _force_cli_args(force)
        return self._outcome(self._run(*args))

    @staticmethod
    def _outcome(cp: subprocess.CompletedProcess) -> Outcome:
        """Map a CLI exit code to the shared structured Outcome.

        exit 0 is success; otherwise the exit code IS the shared engine code
        (10 == concurrency). We synthesize the typed ``ConcurrencyError`` name
        for code 10 so the CLI carries the SAME identity the library raises.
        """
        if cp.returncode == 0:
            return OK
        error_type = "ConcurrencyError" if cp.returncode == CONCURRENCY_CODE else "RebarError"
        return Outcome(ok=False, code=cp.returncode, error_type=error_type)

    def comment(self, tid: str, body: str) -> None:
        assert self._run("comment", tid, body).returncode == 0

    def tag(self, tid: str, tag: str) -> None:
        assert self._run("tag", tid, tag).returncode == 0

    def link(self, a: str, b: str, relation: str) -> None:
        assert self._run("link", a, b, relation).returncode == 0

    def deps(self, tid: str) -> dict:
        return self._ok_json("deps", tid)

    def ready(self) -> Any:
        return self._ok_json("ready", "--output", "json")

    def next_batch(self, epic_id: str) -> dict:
        return self._ok_json("next-batch", epic_id, "--output", "json")

    def search(self, query: str, **filters: Any) -> list[dict]:
        args = ["search", query]
        for k, v in filters.items():
            if v is None:
                continue
            args.append(f"--{k.replace('_', '-')}={v}")
        return self._ok_json(*args)


# ── MCP ──────────────────────────────────────────────────────────────────────
def _parse_block(block: Any) -> Any:
    text = getattr(block, "text", block)
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text


def _unwrap(result: Any) -> Any:
    """Normalize a FastMCP call_tool result to a plain Python value.

    Recent FastMCP returns either a content list (for dict-returning tools) or a
    ``(content_blocks, structured)`` tuple (for list/scalar returns), where
    ``structured`` is ``{"result": <value>}``. Prefer the structured value when
    present; otherwise parse the content block(s).
    """
    if isinstance(result, tuple):
        structured = result[1] if len(result) > 1 else None
        if isinstance(structured, dict):
            if set(structured.keys()) == {"result"}:
                return structured["result"]
            return structured
        result = result[0]
    if not result:
        return None
    if len(result) == 1:
        return _parse_block(result[0])
    return [_parse_block(b) for b in result]


class McpAdapter(Adapter):
    name = "mcp"

    def __init__(self) -> None:
        import asyncio

        from rebar.mcp_server import build_server

        self._asyncio = asyncio
        self._srv = build_server()
        self._schema_cache: dict[str, set[str]] = {}

    def _tool_params(self, tool: str) -> set[str]:
        """The parameter names THIS MCP tool advertises in its inputSchema.

        This is the transport-faithful capability surface — exactly what an MCP
        client sees — so it is the honest signal for "does this surface expose
        the param?". When a sibling change (scratchy-leprous-galago) threads a
        gap param onto the tool, it appears here and the oracle's strict-xfail
        for that row flips to an xpass, forcing the marker's removal.
        """
        if not self._schema_cache:
            for t in self._asyncio.run(self._srv.list_tools()):
                self._schema_cache[t.name] = set((t.inputSchema or {}).get("properties", {}))
        return self._schema_cache.get(tool, set())

    def _call(self, tool: str, **args: Any) -> Any:
        return _unwrap(self._asyncio.run(self._srv.call_tool(tool, args)))

    def create(self, ticket_type: str, title: str, **kw: Any) -> str:
        # create_ticket now returns {id, alias}; normalize to the bare id so the
        # parity protocol (create -> id used downstream) holds across interfaces.
        res = self._call("create_ticket", ticket_type=ticket_type, title=title, **kw)
        return res["id"] if isinstance(res, dict) else res

    def show(self, tid: str) -> dict:
        return self._call("show_ticket", ticket_id=tid)

    def list(self, **filters: Any) -> list[dict]:
        return self._call("list_tickets", **filters)

    def transition(
        self,
        tid: str,
        current: str,
        target: str,
        *,
        force: str | None = None,
        reason: str | None = None,
        close_class: str | None = None,
        caused_by: str | None = None,
        ref: str | None = None,
    ) -> Outcome:
        requested = {
            "force": force,
            "reason": reason,
            "close_class": close_class,
            "caused_by": caused_by,
            "ref": ref,
        }
        exposed = self._tool_params("transition_ticket")
        for name, val in requested.items():
            if val is not None and name not in exposed:
                return NOT_EXPOSED
        args = {"ticket_id": tid, "current_status": current, "target_status": target}
        args.update({k: v for k, v in requested.items() if v is not None})
        try:
            self._call("transition_ticket", **args)
            return OK
        except Exception as exc:  # noqa: BLE001 — parity adapter: any tool failure is uniformly mapped to a structured Outcome via _reject
            return self._reject(exc)

    def claim(self, tid: str, assignee: str | None = None, *, force: str | None = None) -> Outcome:
        if force is not None and "force" not in self._tool_params("claim_ticket"):
            return NOT_EXPOSED
        args: dict[str, Any] = {"ticket_id": tid, "assignee": assignee}
        if force is not None:
            args["force"] = force
        try:
            self._call("claim_ticket", **args)
            return OK
        except Exception as exc:  # noqa: BLE001 — parity adapter: any tool failure is uniformly mapped to a structured Outcome via _reject
            return self._reject(exc)

    @staticmethod
    def _reject(exc: Exception) -> Outcome:
        """Map a FastMCP ToolError to the shared structured Outcome.

        FastMCP wraps the tool's exception in a ToolError chained via ``__cause__``.
        Since 8a31 that cause is an ``McpEnvelopeError`` carrying the structured
        ``error_envelope`` (the SAME machine identity the CLI emits) — its
        ``exit_code`` is the shared code (10 for concurrency) — and the original
        typed ``ConcurrencyError`` is preserved one level deeper on its own
        ``__cause__``. We read the shared code off the envelope and the typed
        identity off the chained engine exception, giving MCP the ONE shared
        structured identity across surfaces.
        """
        from rebar._mcp_errors import McpEnvelopeError

        cause = exc.__cause__ or exc
        if isinstance(cause, McpEnvelopeError):
            engine = cause.__cause__ or cause
            return Outcome(
                ok=False,
                code=cause.envelope.get("exit_code"),
                error_type=type(engine).__name__,
            )
        return Outcome(
            ok=False,
            code=getattr(cause, "returncode", None),
            error_type=type(cause).__name__,
        )

    def comment(self, tid: str, body: str) -> None:
        self._call("comment_ticket", ticket_id=tid, body=body)

    def tag(self, tid: str, tag: str) -> None:
        self._call("tag_ticket", ticket_id=tid, tag=tag)

    def link(self, a: str, b: str, relation: str) -> None:
        self._call("link_tickets", id1=a, id2=b, relation=relation)

    def deps(self, tid: str) -> dict:
        return self._call("ticket_deps", ticket_id=tid)

    def ready(self) -> Any:
        return self._call("ready_tickets")

    def next_batch(self, epic_id: str) -> dict:
        return self._call("next_batch", epic_id=epic_id)

    def search(self, query: str, **filters: Any) -> list[dict]:
        return self._call("search", query=query, **filters)
