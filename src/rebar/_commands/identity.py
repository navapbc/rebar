"""The ``identity`` entity: create path, the self-identity pointer, and resolver.

An ``identity`` ticket is a first-class, gate-/graph-exempt entity (modeled on
``session_log`` / ``code_review``) that records a person/agent: a ``name`` (its
title), an ``email``, external-provider ``mappings`` (``{provider, external_id}``),
and OpenSSH authorized-``keys`` lines. All three payload fields ride the CREATE
event so the reducer surfaces them in compiled state.

The "current" identity is tracked by a LOCAL, git-ignored pointer file
(``<repo>/.rebar/current_identity`` — the same ``.rebar`` local-state root
``session_log`` / ``scratch`` use), so it never enters the shared tickets branch and
never propagates across machines. Identity is entirely OPT-IN: an unauthenticated
checkout (no pointer, no matching git email) is valid, so :func:`resolve_current_identity`
returns ``None`` on every miss and NEVER raises.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path

from rebar._commands._seam import CommandError, tracker_dir
from rebar._commands.composer import create_core

logger = logging.getLogger(__name__)

_POINTER_NAME = "current_identity"


# ── pointer file (mirrors session_log's local, git-ignored pointer) ────────────
def _pointer_path(repo_root=None) -> Path:
    from rebar import config

    root = repo_root or config.repo_root()
    return Path(root) / ".rebar" / _POINTER_NAME


def _read_pointer_id(repo_root=None) -> str | None:
    """Read the ``identity_id`` from the pointer file (JSON ``{"identity_id": ...}``).

    Tolerates extra keys. Any read/parse failure or a missing/blank id yields
    ``None`` (a corrupt pointer is a miss, not an error)."""
    try:
        raw = _pointer_path(repo_root).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if isinstance(obj, dict) and obj.get("identity_id"):
        return str(obj["identity_id"])
    return None


def _write_pointer(identity_id: str, repo_root=None) -> None:
    from rebar._store.fsutil import atomic_write

    p = _pointer_path(repo_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"identity_id": identity_id}, ensure_ascii=False)
    atomic_write(p, payload, encoding="utf-8")  # atomic publish


# ── identity ticket lookups ────────────────────────────────────────────────────
def _reduce(ticket_dir: str) -> dict | None:
    from rebar.reducer import reduce_ticket

    try:
        return reduce_ticket(ticket_dir)
    except Exception:  # noqa: BLE001 — an unreadable/corrupt ticket is simply "not an identity"
        return None


def _is_identity(ticket_id: str, tracker: str) -> bool:
    """True iff ``ticket_id`` is an existing, non-deleted ``identity`` ticket."""
    import os

    d = os.path.join(tracker, ticket_id)
    if not os.path.isdir(d):
        return False
    state = _reduce(d)
    return (
        isinstance(state, dict)
        and state.get("ticket_type") == "identity"
        and state.get("status") != "deleted"
    )


def _git_email(repo_root=None) -> str | None:
    """``git config user.email`` for the store repo, or ``None`` on ANY failure.

    Identity is opt-in, so an unset email, a missing/failed ``git`` invocation, a
    non-zero exit, or a timeout all degrade to ``None`` (never raise)."""
    from rebar import config

    root = str(repo_root or config.repo_root())
    try:
        proc = subprocess.run(
            ["git", "config", "user.email"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    email = (proc.stdout or "").strip()
    return email or None


def _match_by_email(email: str, tracker: str) -> str | None:
    """Return the sole identity id whose ``email`` matches (case-insensitive), else
    ``None`` (zero matches OR two-or-more ambiguous matches both resolve to None)."""
    import os

    target = email.strip().lower()
    matches: list[str] = []
    try:
        entries = sorted(os.listdir(tracker))
    except OSError:
        return None
    for entry in entries:
        if entry.startswith("."):
            continue
        d = os.path.join(tracker, entry)
        if not os.path.isdir(d):
            continue
        state = _reduce(d)
        if not isinstance(state, dict) or state.get("ticket_type") != "identity":
            continue
        if state.get("status") == "deleted":
            continue
        got = state.get("email")
        if isinstance(got, str) and got.strip().lower() == target:
            matches.append(state.get("ticket_id") or entry)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        logger.warning(
            "resolve_current_identity: %d identities match git email %r (ambiguous) — "
            "returning None; disambiguate with `rebar identity use <id>`",
            len(matches),
            email,
        )
    return None


# ── public core functions ──────────────────────────────────────────────────────
def create_identity_core(
    name: str,
    email: str,
    mappings: list[dict] | None = None,
    keys: list[str] | None = None,
    *,
    repo_root=None,
) -> dict:
    """Mint an ``identity`` ticket in ONE CREATE event; return ``{id, alias, title}``.

    ``name`` becomes the title; ``email`` / ``mappings`` / ``keys`` ride the CREATE
    payload (see :func:`rebar._commands.composer.create_core`). Raises
    :class:`CommandError` on validation failure."""
    return create_core(
        "identity",
        name,
        description="",
        identity={
            "email": email,
            "mappings": mappings or [],
            "keys": keys or [],
        },
        repo_root=repo_root,
    )


def use_identity(identity_id: str, *, repo_root=None) -> None:
    """Write the ``.rebar/current_identity`` pointer to ``identity_id``.

    Invalidates the per-repo attribution cache (epic gnu-whale-ichor): the current
    identity determines the ``author_id`` stamped on subsequent event envelopes, so a
    pointer change must be picked up by the next ``attribution_fields`` call rather
    than served a stale cached dict computed under the previous identity."""
    _write_pointer(identity_id, repo_root=repo_root)
    from rebar._commands._seam import _reset_attribution_cache

    _reset_attribution_cache()


def resolve_current_identity(*, repo_root=None) -> str | None:
    """Resolve the current self-identity, or ``None`` (opt-in; never raises).

    Order: (1) if the pointer names an existing ``identity`` ticket, return it;
    (2) else a case-insensitive ``git config user.email`` match against identity
    tickets. Every miss — no pointer / deleted target, git email unset or the ``git``
    call itself failing, zero matches, or two-or-more ambiguous matches — is ``None``."""
    tracker = str(tracker_dir(repo_root))
    pointer_id = _read_pointer_id(repo_root)
    if pointer_id and _is_identity(pointer_id, tracker):
        return pointer_id
    email = _git_email(repo_root)
    if not email:
        return None
    return _match_by_email(email, tracker)


# ───────────────────────────────── CLI ───────────────────────────────────────
_USAGE = (
    "Usage: rebar identity <create | use>\n"
    "  create --name <n> --email <e> [--mapping <provider>:<external_id>]... "
    '[--key "<authorized-keys line>"]... [--self]\n'
    "  use <id>"
)


def _parse_create(argv: list[str]) -> dict:
    name = email = None
    mappings: list[dict] = []
    keys: list[str] = []
    use_self = False
    i, n = 0, len(argv)

    def _val(flag: str, idx: int) -> tuple[str, int]:
        if argv[idx].startswith(flag + "="):
            return argv[idx][len(flag) + 1 :], idx + 1
        if idx + 1 >= n:
            raise CommandError(f"Error: {flag} requires a value\n{_USAGE}")
        return argv[idx + 1], idx + 2

    while i < n:
        a = argv[i]
        if a == "--name" or a.startswith("--name="):
            name, i = _val("--name", i)
        elif a == "--email" or a.startswith("--email="):
            email, i = _val("--email", i)
        elif a == "--mapping" or a.startswith("--mapping="):
            raw, i = _val("--mapping", i)
            if ":" not in raw:
                raise CommandError(
                    f"Error: --mapping must be <provider>:<external_id> (got '{raw}')\n{_USAGE}"
                )
            provider, external_id = raw.split(":", 1)
            mappings.append({"provider": provider, "external_id": external_id})
        elif a == "--key" or a.startswith("--key="):
            raw, i = _val("--key", i)
            keys.append(raw)
        elif a == "--self":
            use_self = True
            i += 1
        else:
            raise CommandError(f"Error: unknown option '{a}'\n{_USAGE}")
    if not name:
        raise CommandError(f"Error: --name is required\n{_USAGE}")
    if not email:
        raise CommandError(f"Error: --email is required\n{_USAGE}")
    return {
        "name": name,
        "email": email,
        "mappings": mappings,
        "keys": keys,
        "use_self": use_self,
    }


def identity_cli(argv: list[str], *, repo_root=None) -> int:
    """``rebar identity create ...`` / ``rebar identity use <id>``."""
    if not argv or argv[0] in ("--help", "-h", "help"):
        print(_USAGE)
        return 0 if argv else 1
    verb, rest = argv[0], argv[1:]
    try:
        if verb == "create":
            opts = _parse_create(rest)
            res = create_identity_core(
                opts["name"],
                opts["email"],
                mappings=opts["mappings"],
                keys=opts["keys"],
                repo_root=repo_root,
            )
            if opts["use_self"]:
                use_identity(res["id"], repo_root=repo_root)
            alias, tid = res.get("alias"), res["id"]
            if alias and alias != tid:
                print(f"Created identity {alias} ({tid}): {res['title']}")
            else:
                print(f"Created identity {tid}: {res['title']}")
            print(tid)  # last whitespace-token = id (mirrors `create`)
            return 0
        if verb == "use":
            if len(rest) != 1:
                raise CommandError(f"Error: 'use' requires exactly one <id>\n{_USAGE}")
            use_identity(rest[0], repo_root=repo_root)
            print(f"Now using identity {rest[0]}")
            return 0
        print(f"Error: unknown identity action '{verb}'\n{_USAGE}", file=sys.stderr)
        return 1
    except CommandError as exc:
        print(exc.message, file=sys.stderr)
        return exc.returncode
