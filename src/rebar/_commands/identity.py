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

from rebar._commands._seam import CommandError, append_event, tracker_dir
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


# ── provider-neutral resolution seam (epic gnu-whale-ichor / 264f) ──────────────
def _iter_identities(repo_root=None):
    """Yield ``(ticket_id, state)`` for every non-deleted ``identity`` ticket.

    A thin generator over the tracker directory (mirrors :func:`_match_by_email`'s
    scan) shared by the provider-neutral resolvers below. Silent on any listdir /
    reduce failure — the resolvers are opt-in and never raise."""
    import os

    tracker = str(tracker_dir(repo_root))
    try:
        entries = sorted(os.listdir(tracker))
    except OSError:
        return
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
        yield (state.get("ticket_id") or entry, state)


def _match_identity(local_assignee, repo_root=None) -> dict | None:
    """The identity whose ticket id == ``local_assignee`` OR whose ``email`` matches it
    (case-insensitive), else ``None``. An id match wins immediately; an email match must
    be UNIQUE (zero or ≥2 email matches → ``None``, mirroring :func:`_match_by_email`).
    Never raises."""
    if not isinstance(local_assignee, str) or not local_assignee.strip():
        return None
    target = local_assignee.strip()
    target_lower = target.lower()
    email_matches: list[dict] = []
    try:
        for tid, state in _iter_identities(repo_root):
            if state.get("ticket_id") == target or tid == target:
                return state
            email = state.get("email")
            if isinstance(email, str) and email.strip().lower() == target_lower:
                email_matches.append(state)
    except Exception:  # noqa: BLE001 — opt-in resolver: any scan failure is a miss, not an error
        return None
    return email_matches[0] if len(email_matches) == 1 else None


def resolve_mapping(provider: str, external_id: str, *, repo_root=None) -> str | None:
    """Id of the identity whose ``mappings`` contains ``{provider, external_id}`` (an
    EXACT match on the provider's opaque external id, NEVER email), else ``None``.

    The provider-neutral seam (264f): 2f13's inbound ghost-minting and the Jira
    outbound adapter both key on the opaque id through here. Never raises."""
    try:
        for tid, state in _iter_identities(repo_root):
            for m in state.get("mappings") or []:
                if not isinstance(m, dict):
                    continue
                if m.get("provider") == provider and m.get("external_id") == external_id:
                    return tid
    except Exception:  # noqa: BLE001 — opt-in resolver: any scan failure is a miss, not an error
        return None
    return None


def jira_account_id(local_assignee: str, *, repo_root=None) -> str | None:
    """Resolve a LOCAL assignee/reporter string to its Jira accountId, else ``None``.

    Matches an identity by ticket id or case-insensitive ``email`` (see
    :func:`_match_identity`) and returns that identity's ``{provider: "jira"}``
    ``external_id`` (the opaque accountId). Never raises."""
    state = _match_identity(local_assignee, repo_root=repo_root)
    if state is None:
        return None
    for m in state.get("mappings") or []:
        if isinstance(m, dict) and m.get("provider") == "jira":
            ext = m.get("external_id")
            if isinstance(ext, str) and ext:
                return ext
    return None


def identity_email(local_assignee: str, *, repo_root=None) -> str | None:
    """The matched identity's ``email`` (same id/email matching as
    :func:`jira_account_id`), else ``None`` — the seam the ``/user/search`` outbound
    bootstrap uses to obtain an email to query. Never raises."""
    state = _match_identity(local_assignee, repo_root=repo_root)
    if state is None:
        return None
    email = state.get("email")
    return email if isinstance(email, str) and email else None


# ── public core functions ──────────────────────────────────────────────────────
# ── write-time private-key guard (401a, epic gnu-whale-ichor) ───────────────────
def reject_private_key_material(values: list[str]) -> None:
    """Refuse (raise :class:`CommandError`) if any string in ``values`` carries a
    private-key PEM/OpenSSH header.

    An ``identity`` records only PUBLIC key material (authorized-keys lines); a private
    key must never be written into an identity event. Detection is case-INSENSITIVE and
    covers the ``-----BEGIN … PRIVATE KEY-----`` header family (OpenSSH, RSA, EC, DSA,
    plain, and encoded/encrypted). A normal ``ssh-ed25519 AAAA…`` public line does NOT
    match. Non-``str`` entries are ignored (defensive)."""
    for v in values:
        if not isinstance(v, str):
            continue
        low = v.lower()
        if "private key-----" in low and "-----begin" in low:
            raise CommandError(
                "Error: refusing to store private-key material in an identity "
                "(identities record public keys only)"
            )


def create_identity_core(
    name: str,
    email: str,
    mappings: list[dict] | None = None,
    keys: list[str] | None = None,
    *,
    tags: list[str] | None = None,
    repo_root=None,
    creation_channel: str = "python",
) -> dict:
    """Mint an ``identity`` ticket in ONE CREATE event; return ``{id, alias, title}``.

    ``name`` becomes the title; ``email`` / ``mappings`` / ``keys`` ride the CREATE
    payload (see :func:`rebar._commands.composer.create_core`). ``tags`` (e.g. a
    ``placeholder`` marker for a ghost identity) rides the SAME CREATE event so it is
    atomic — no separate ``tag`` call, no tagless window. Raises :class:`CommandError`
    on validation failure.

    ``creation_channel`` (story 6fe2) is threaded to the genesis CREATE. This is the
    SHARED signature local/library callers reach through the ``"python"`` default and
    ``identity_cli`` overrides to ``"cli"``; a later Jira story supplies ``"jira"`` at
    the inbound boundary."""
    reject_private_key_material(keys or [])
    return create_core(
        "identity",
        name,
        description="",
        tags=tags,
        identity={
            "email": email,
            "mappings": mappings or [],
            "keys": keys or [],
        },
        repo_root=repo_root,
        creation_channel=creation_channel,
    )


_PLACEHOLDER_TAG = "placeholder"


def is_placeholder(identity_id: str, *, repo_root=None) -> bool:
    """True iff ``identity_id`` is an identity whose compiled-state ``tags`` carries the
    ``placeholder`` marker (a ghost minted for an unmapped inbound user). An unknown id,
    a non-identity ticket, or any reduce failure is ``False`` — never raises."""
    import os

    if not isinstance(identity_id, str) or not identity_id.strip():
        return False
    try:
        tracker = str(tracker_dir(repo_root))
        d = os.path.join(tracker, identity_id)
        state = _reduce(d)
        if not isinstance(state, dict) or state.get("ticket_type") != "identity":
            return False
        return _PLACEHOLDER_TAG in (state.get("tags") or [])
    except Exception:  # noqa: BLE001 — opt-in read: any failure is "not a placeholder"
        return False


def ensure_identity_for(
    provider: str,
    external_id: str,
    display_name: str,
    *,
    repo_root=None,
    creation_channel: str = "python",
) -> str:
    """Resolve-or-mint the identity for an inbound ``(provider, external_id)`` user; return
    its id (2f13, epic gnu-whale-ichor). Idempotent and provider-neutral:

    * RESOLVE FIRST via :func:`resolve_mapping` (keyed on the opaque external id, NEVER
      email). If an identity already carries this mapping, RETURN it — never mint a
      second. When that existing identity is still a *placeholder* and ``display_name`` is
      non-empty and differs from its current title, UPGRADE the title IN PLACE (a real,
      already-named identity is left untouched — a ghost's mapping is reused, never
      renamed over a human's).
    * Else MINT a placeholder: an ``identity`` whose title is ``display_name`` (falling
      back to ``external_id`` when blank), an empty email, the single ``{provider,
      external_id}`` mapping, and the ``placeholder`` tag riding the SAME CREATE event.

    Never raises on a lookup problem — a resolve failure falls through to a mint.

    ``creation_channel`` (story 6fe2) is threaded to a minted placeholder's genesis
    CREATE; it defaults to ``"python"`` (a later Jira story supplies ``"jira"`` at the
    inbound boundary)."""
    import os

    existing = None
    try:
        existing = resolve_mapping(provider, external_id, repo_root=repo_root)
    except Exception:  # noqa: BLE001 — a resolve failure falls through to mint, never raises
        existing = None

    if existing:
        name = (display_name or "").strip()
        if name and is_placeholder(existing, repo_root=repo_root):
            state = _reduce(os.path.join(str(tracker_dir(repo_root)), existing))
            current_title = (state or {}).get("title") if isinstance(state, dict) else None
            if name != current_title:
                try:
                    import rebar

                    rebar.edit_ticket(existing, title=name, repo_root=repo_root)
                except Exception:  # noqa: BLE001 — a best-effort upgrade never fails the resolve
                    logger.warning(
                        "ensure_identity_for: could not upgrade placeholder %s title",
                        existing,
                    )
        return existing

    res = create_identity_core(
        (display_name or "").strip() or external_id,
        "",
        mappings=[{"provider": provider, "external_id": external_id}],
        tags=[_PLACEHOLDER_TAG],
        repo_root=repo_root,
        creation_channel=creation_channel,
    )
    return res["id"]


def create_placeholder(
    provider: str,
    external_id: str,
    display_name: str,
    *,
    repo_root=None,
) -> str:
    """Resolve-or-mint the placeholder identity for ``(provider, external_id)``; return its id
    (117b). A thin, intention-revealing alias for :func:`ensure_identity_for` — same
    idempotent resolve-first-else-mint semantics, never raises on a lookup problem."""
    return ensure_identity_for(provider, external_id, display_name, repo_root=repo_root)


def _identity_state(identity_id: str, *, repo_root=None) -> dict:
    """Reduced state of an existing ``identity`` ticket, or raise :class:`CommandError`.

    Fails closed: an unknown id, a non-identity ticket, or a reduce failure is a
    :class:`CommandError` (the key-lifecycle gate must never operate on a non-identity)."""
    import rebar

    try:
        state = rebar.show_ticket(identity_id, repo_root=repo_root)
    except Exception as exc:  # noqa: BLE001 — surface any lookup failure as a command error
        raise CommandError(f"Error: identity {identity_id!r} not found: {exc}") from None
    if not isinstance(state, dict) or state.get("ticket_type") != "identity":
        raise CommandError(f"Error: {identity_id!r} is not an identity ticket")
    return dict(state)


def _encode_signature(signature) -> str:
    """Serialize a DSSE :class:`~rebar.attest.dsse.Envelope` to its canonical JSON string
    for durable, auditable storage on the KEY event (round-trips via ``dsse.decode``)."""
    from rebar.attest import dsse

    return dsse.encode(
        signature.payload_type,
        signature.payload,
        [{"keyid": s.keyid, "sig": s.sig} for s in signature.signatures],
    )


def _verify_keyop_signature(op: str, identity_id: str, public_key: str, signature, *, repo_root):
    """Refuse (raise :class:`CommandError`) unless ``signature`` is a valid authorship
    attestation over ``keyop_payload(op, identity_id, public_key)`` by a CURRENTLY-valid
    key of ``identity_id``. Two independent checks, both required:

    1. ``signature.payload`` is EXACTLY the canonical key-op payload — so a signature over
       some other op / identity / key can't be replayed here.
    2. :func:`authorship.verify_authorship` verifies it against the identity's currently
       valid keys — so an outsider (or a revoked key) cannot authorize the rotation.
    """
    from rebar.attest import authorship, dsse

    if not isinstance(signature, dsse.Envelope):
        raise CommandError(
            f"Error: {op} requires a signature (dsse.Envelope) for a non-genesis key"
        )
    expected = authorship.keyop_payload(op, identity_id, public_key)
    if signature.payload != expected:
        raise CommandError(
            f"Error: {op} signature does not cover this key operation "
            f"(payload mismatch for identity {identity_id!r})"
        )
    verdict = authorship.verify_authorship(signature, identity_id, repo_root=repo_root)
    if not verdict.verified:
        raise CommandError(
            f"Error: {op} signature is not signed by a currently-valid key of "
            f"identity {identity_id!r} ({verdict.verdict}: {verdict.reason})"
        )


def add_identity_key(
    identity_id: str,
    public_key: str,
    *,
    signature=None,
    repo_root=None,
) -> None:
    """Add ``public_key`` to an identity's keyring (epic gnu-whale-ichor / e165).

    GENESIS / TOFU: when the identity currently has NO valid keys, the first key is added
    trust-on-first-use — no signature is required (there is no prior key that could sign
    it). NON-GENESIS: once the identity holds at least one valid key, ``signature`` (a
    :class:`~rebar.attest.dsse.Envelope` over ``keyop_payload("KEY_ADD", identity_id,
    public_key)``) is REQUIRED and must verify against a currently-valid key; otherwise the
    rotation is REFUSED and NO event is appended. On success a ``KEY_ADD`` event is
    appended (the signature envelope, if any, is stored encoded for auditability)."""
    if not isinstance(public_key, str) or not public_key.strip():
        raise CommandError("Error: add_identity_key requires a non-empty public key")
    public_key = public_key.strip()
    reject_private_key_material([public_key])
    state = _identity_state(identity_id, repo_root=repo_root)
    is_genesis = not (state.get("keys") or [])
    encoded_sig = None
    if not is_genesis:
        _verify_keyop_signature("KEY_ADD", identity_id, public_key, signature, repo_root=repo_root)
        encoded_sig = _encode_signature(signature)
    append_event(
        identity_id,
        "KEY_ADD",
        {"public_key": public_key, "signature": encoded_sig, "op": "KEY_ADD"},
        tracker_dir(repo_root),
        repo_root=repo_root,
    )


def revoke_identity_key(
    identity_id: str,
    public_key: str,
    *,
    signature,
    repo_root=None,
) -> None:
    """Revoke ``public_key`` from an identity's keyring (epic gnu-whale-ichor / e165).

    A revoke is ALWAYS signed: ``signature`` (a :class:`~rebar.attest.dsse.Envelope` over
    ``keyop_payload("KEY_REVOKE", identity_id, public_key)``) is REQUIRED and must verify
    against a currently-valid key of the identity; otherwise the revoke is REFUSED and NO
    event is appended. On success a ``KEY_REVOKE`` event is appended, closing the key's
    validity window at the revoke event's position (its introducing commit)."""
    if not isinstance(public_key, str) or not public_key.strip():
        raise CommandError("Error: revoke_identity_key requires a non-empty public key")
    public_key = public_key.strip()
    _identity_state(identity_id, repo_root=repo_root)
    _verify_keyop_signature("KEY_REVOKE", identity_id, public_key, signature, repo_root=repo_root)
    append_event(
        identity_id,
        "KEY_REVOKE",
        {"public_key": public_key, "signature": _encode_signature(signature), "op": "KEY_REVOKE"},
        tracker_dir(repo_root),
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
    "Usage: rebar identity <create | use | key>\n"
    "  create --name <n> --email <e> [--mapping <provider>:<external_id>]... "
    '[--key "<authorized-keys line>"]... [--self]\n'
    "  use <id>\n"
    '  key add <id> "<authorized-keys line>" [--signature-file <path>]\n'
    '  key revoke <id> "<authorized-keys line>" --signature-file <path>'
)


def _load_signature(path: str | None):
    """Decode a DSSE :class:`~rebar.attest.dsse.Envelope` from a ``dsse.encode`` JSON file,
    or ``None`` when no ``--signature-file`` was given (genesis add). Raises
    :class:`CommandError` on a missing/malformed file."""
    if path is None:
        return None
    from rebar.attest import dsse

    try:
        text = Path(path).read_text(encoding="utf-8")
        return dsse.decode(text)
    except (OSError, ValueError, KeyError) as exc:
        raise CommandError(f"Error: could not read signature file {path!r}: {exc}") from None


def _parse_key(argv: list[str]) -> dict:
    """Parse ``key <add|revoke> <id> <pubkey> [--signature-file <path>]``."""
    if len(argv) < 3:
        raise CommandError(f"Error: 'key' requires <add|revoke> <id> <pubkey>\n{_USAGE}")
    action, identity_id, public_key = argv[0], argv[1], argv[2]
    if action not in ("add", "revoke"):
        raise CommandError(f"Error: unknown key action '{action}' (add|revoke)\n{_USAGE}")
    sig_file: str | None = None
    i, n = 3, len(argv)
    while i < n:
        a = argv[i]
        if a == "--signature-file" or a.startswith("--signature-file="):
            if a.startswith("--signature-file="):
                sig_file, i = a[len("--signature-file=") :], i + 1
            elif i + 1 >= n:
                raise CommandError(f"Error: --signature-file requires a value\n{_USAGE}")
            else:
                sig_file, i = argv[i + 1], i + 2
        else:
            raise CommandError(f"Error: unknown option '{a}'\n{_USAGE}")
    return {
        "action": action,
        "identity_id": identity_id,
        "public_key": public_key,
        "sig_file": sig_file,
    }


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
                creation_channel="cli",
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
        if verb == "key":
            opts = _parse_key(rest)
            signature = _load_signature(opts["sig_file"])
            if opts["action"] == "add":
                add_identity_key(
                    opts["identity_id"],
                    opts["public_key"],
                    signature=signature,
                    repo_root=repo_root,
                )
                print(f"Added key to identity {opts['identity_id']}")
            else:
                if signature is None:
                    raise CommandError(f"Error: key revoke requires --signature-file\n{_USAGE}")
                revoke_identity_key(
                    opts["identity_id"],
                    opts["public_key"],
                    signature=signature,
                    repo_root=repo_root,
                )
                print(f"Revoked key from identity {opts['identity_id']}")
            return 0
        print(f"Error: unknown identity action '{verb}'\n{_USAGE}", file=sys.stderr)
        return 1
    except CommandError as exc:
        print(exc.message, file=sys.stderr)
        return exc.returncode
