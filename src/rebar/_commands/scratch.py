"""In-process ``scratch`` set/get/clear.

Scratch is a filesystem-only per-ticket key/value store under
``<repo>/.rebar/scratch/`` (``scratch.base_dir`` / ``REBAR_SCRATCH_BASE_DIR``
overrides) — NO ticket store, NO
write lock, NO auto-init. Values are wrapped ``{"ts":<iso8601>,"value":<str>}`` and
written atomically (same-dir tmp + fsync + rename). All structured output is JSON on
stdout (default ``json.dumps`` separators, except the compact ``unknown_verb``
envelope).

``base_dir`` / ``cleanup_for_ticket`` are the shared scratch-location helpers that
the transition and delete close paths reuse to purge a ticket's scratch dir.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile

from rebar import config
from rebar.timeutils import utc_now_iso

_MAX_BYTES = 98304
_CONTROL_RE = re.compile(r"[\x00-\x1f]")


def base_dir(repo_root=None) -> str:
    """The scratch base directory: ``scratch.base_dir`` (env REBAR_SCRATCH_BASE_DIR,
    deprecated alias SCRATCH_BASE_DIR, or a config file) if set, else
    ``<repo_root or config.repo_root()>/.rebar/scratch``. Scratch is best-effort
    infra, so a malformed config falls back to the default rather than erroring."""
    try:
        base = config.load_config(repo_root).scratch.base_dir
    except config.ConfigError:
        base = ""
    if base:
        return base
    root = repo_root if repo_root is not None else str(config.repo_root())
    return os.path.join(str(root), ".rebar", "scratch")


def cleanup_for_ticket(repo_root, ticket_id: str) -> None:
    """Best-effort removal of a ticket's scratch dir (silenced)."""
    try:
        shutil.rmtree(os.path.join(base_dir(repo_root), ticket_id), ignore_errors=True)
    except OSError:
        pass


def _base_dir() -> str:
    return base_dir()


def _validate_component(value: str, field_name: str, code: str) -> dict | None:
    """Return an error-envelope dict if ``value`` is empty / leading-dot / contains
    ``..`` / ``/`` / control chars; else None. Mirrors _scratch_resolve_and_validate."""
    if not value:
        return {"status": "error", "code": code, "reason": f"{field_name} must not be empty"}
    if value.startswith("."):
        return {
            "status": "error",
            "code": code,
            "reason": f"{field_name} must not start with a dot: {value!r}",
        }
    if ".." in value:
        return {
            "status": "error",
            "code": code,
            "reason": f"{field_name} must not contain '..': {value!r}",
        }
    if "/" in value:
        return {
            "status": "error",
            "code": code,
            "reason": f"{field_name} must not contain '/': {value!r}",
        }
    if _CONTROL_RE.search(value):
        return {
            "status": "error",
            "code": code,
            "reason": f"{field_name} must not contain control characters: {value!r}",
        }
    return None


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")


def _resolve_and_validate(ticket_id: str, key: str) -> tuple[str | None, int]:
    """Return (abs_path, 0) on valid inputs, or (None, 1) after emitting the error
    envelope (ticket_id checked first, then key)."""
    err = _validate_component(ticket_id, "ticket_id", "invalid_id")
    if err is None:
        err = _validate_component(key, "key", "invalid_key")
    if err is not None:
        _emit(err)
        return None, 1
    return os.path.join(_base_dir(), ticket_id, key), 0


def _set(args: list[str]) -> int:
    if len(args) < 3:
        sys.stderr.write(
            "Usage: ticket scratch set <ticket_id> <key> <value>\n"
            "  ticket_id : per-ticket namespace\n"
            "  key       : scratch key name\n"
            "  value     : payload string to store\n"
        )
        return 1
    ticket_id, key, value = args[0], args[1], args[2]
    abs_path, rc = _resolve_and_validate(ticket_id, key)
    if abs_path is None:
        return rc

    ts = utc_now_iso()
    envelope = json.dumps({"ts": ts, "value": value})
    payload = envelope.encode("utf-8")
    if len(payload) > _MAX_BYTES:
        _emit({"status": "error", "code": "oversize", "limit": _MAX_BYTES, "actual": len(payload)})
        return 1

    target_dir = os.path.dirname(abs_path)
    os.makedirs(target_dir, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=target_dir, prefix=os.path.basename(abs_path) + ".tmp.", suffix=".scratch"
    )
    try:
        os.write(fd, payload)
        os.fsync(fd)
        os.close(fd)
        os.rename(tmp_path, abs_path)
        dir_fd = os.open(target_dir, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        except OSError:
            pass
        finally:
            os.close(dir_fd)
    except Exception as exc:
        for cleanup in (lambda: os.close(fd), lambda: os.unlink(tmp_path)):
            try:
                cleanup()
            except OSError:
                pass
        sys.stderr.write(f"Error: atomic write failed: {exc}\n")
        return 2

    _emit({"status": "ok", "ticket_id": ticket_id, "key": key})
    return 0


def _get(args: list[str]) -> int:
    if len(args) < 2:
        sys.stderr.write(
            "Usage: ticket scratch get <ticket_id> <key>\n"
            "  ticket_id: ticket namespace identifier\n"
            "  key:       scratch key name\n"
        )
        return 1
    ticket_id, key = args[0], args[1]
    abs_path, rc = _resolve_and_validate(ticket_id, key)
    if abs_path is None:
        return rc

    if not os.path.isfile(abs_path):
        _emit({"status": "miss", "ticket_id": ticket_id, "key": key})
        return 0
    try:
        with open(abs_path, encoding="utf-8") as f:
            content = f.read()
    except OSError:
        _emit({"status": "miss", "ticket_id": ticket_id, "key": key})
        return 0
    if not content:
        _emit({"status": "miss", "ticket_id": ticket_id, "key": key})
        return 0

    try:
        stored = json.loads(content)
    except json.JSONDecodeError as exc:
        _emit(
            {
                "status": "error",
                "code": "malformed_envelope",
                "reason": str(exc),
                "ticket_id": ticket_id,
                "key": key,
            }
        )
        return 1
    _emit({"status": "hit", "ts": stored.get("ts", ""), "value": stored.get("value", "")})
    return 0


def _clear(args: list[str]) -> int:
    ticket_id = args[0] if args else ""
    key = args[1] if len(args) > 1 else ""
    if not ticket_id:
        # Compact JSON (matches the bash printf template, not json.dumps).
        sys.stdout.write(
            '{"status":"error","code":"missing_args",'
            '"reason":"Usage: ticket-scratch-clear.sh <ticket_id> [<key>]"}\n'
        )
        return 1

    if key:
        abs_path, rc = _resolve_and_validate(ticket_id, key)
        if abs_path is None:
            return rc
        removed = 0
        if os.path.isfile(abs_path):
            os.remove(abs_path)
            removed = 1
        _emit({"status": "ok", "ticket_id": ticket_id, "key": key, "removed": removed})
        return 0

    # Whole-ticket mode: validate ticket_id only.
    err = _validate_component(ticket_id, "ticket_id", "invalid_id")
    if err is not None:
        _emit(err)
        return 1
    ticket_dir = os.path.join(_base_dir(), ticket_id)
    removed = 0
    if os.path.isdir(ticket_dir):
        removed = sum(1 for e in os.scandir(ticket_dir) if e.is_file())
        shutil.rmtree(ticket_dir)
    _emit({"status": "ok", "ticket_id": ticket_id, "removed": removed})
    return 0


def scratch_cli(argv: list[str], *, repo_root=None) -> int:
    """``rebar scratch <verb> [args...]`` — route set/get/clear; unknown verb →
    compact JSON error (matching the bash printf)."""
    if len(argv) < 1:
        sys.stderr.write(
            "Usage: ticket scratch <verb> [args...]\n\n"
            "Verbs:\n"
            "  set   <ticket_id> <key> <value>  — write a scratch key\n"
            "  get   <ticket_id> <key>           — read a scratch key\n"
            "  clear <ticket_id> [<key>]         — remove a key or entire ticket scratch\n"
        )
        return 1
    verb, rest = argv[0], argv[1:]
    if verb == "set":
        return _set(rest)
    if verb == "get":
        return _get(rest)
    if verb == "clear":
        return _clear(rest)
    sys.stdout.write(f'{{"status":"error","code":"unknown_verb","verb":"{verb}"}}\n')
    return 1
