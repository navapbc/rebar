"""Authorship attestation kind (``rebar.authorship.v1``) on the SSHSIG substrate.

Epic gnu-whale-ichor: an author signs an event payload with their SSH private
key; anyone verifies it against the author's **in-band** public keys (the
``keys`` recorded on that author's ``identity`` ticket) and the pinned
``rebar.authorship.v1`` namespace. Built entirely on the foundation asymmetric
attest substrate — DSSE PAE envelope + the ``sshsig`` scheme + the per-kind
policy table — so it inherits the substrate's fail-closed / no-alg-confusion
guarantees:

* The scheme is pinned by policy (``sshsig``), never chosen from the envelope.
* The trust root is the author identity's own ``keys`` bound to that identity's
  id as the SSHSIG principal — so a payload signed by identity A verifies only
  against A, never against a different identity B (B's keys/principal are a
  disjoint trust root).
* Any identity-lookup problem (unknown id, corrupt store, I/O error, a keyless
  identity) resolves to a non-verified ``Verdict`` — never an exception a caller
  might mistake for a pass.
"""

from __future__ import annotations

import os
import subprocess
from typing import cast

from rebar.attest import dsse, registry, sshsig

AUTHORSHIP_KIND = "rebar.authorship.v1"
AUTHORSHIP_NAMESPACE = "rebar.authorship.v1"
PAYLOAD_TYPE = "application/vnd.rebar.authorship.v1+json"


def register_authorship_policy() -> None:
    """Pin the ``rebar.authorship.v1`` kind to the ``sshsig`` scheme (idempotent)."""
    registry.POLICY[AUTHORSHIP_KIND] = registry.Policy(
        scheme="sshsig",
        namespace=AUTHORSHIP_NAMESPACE,
    )


def sign_authorship(payload: bytes, key_path: str, principal: str) -> dsse.Envelope:
    """Sign ``payload`` as an authorship attestation by ``principal``.

    ``ssh-keygen`` availability is asserted first so signing is honest — a
    missing/too-old ssh-keygen raises :class:`sshsig.SshKeygenUnavailable`
    rather than producing an unverifiable envelope. The signature is taken over
    the DSSE-PAE bytes of ``(PAYLOAD_TYPE, payload)`` under the pinned
    authorship namespace; ``principal`` (the author identity's id) becomes the
    signature ``keyid`` (the SSHSIG principal the verifier binds against).
    """
    sshsig.ensure_available()
    pae = dsse.pae(PAYLOAD_TYPE, payload)
    sig = sshsig.sign(pae, key_path, AUTHORSHIP_NAMESPACE)
    return dsse.Envelope(
        PAYLOAD_TYPE,
        payload,
        [dsse.Signature(keyid=principal, sig=sig)],
    )


def allowed_signers_from_keys(keys: list[str], principal: str) -> str:
    """Render an OpenSSH ``allowed_signers`` file binding ``keys`` to ``principal``.

    Each ``key_line`` is an authorized-keys line (``"ssh-ed25519 AAAA..."``);
    the emitted line is ``"<principal> <key_line>"``. Blank / whitespace-only
    entries are skipped. Lines are joined with a single newline.
    """
    return "\n".join(
        f"{principal} {key_line.strip()}" for key_line in keys if key_line and key_line.strip()
    )


def resolve_trust_root(identity_id: str, *, repo_root=None) -> str | None:
    """Compute the SSHSIG trust root for author ``identity_id``, or ``None``.

    Looks up the identity ticket and, if it is an ``identity`` with a non-empty
    ``keys`` list, returns an ``allowed_signers`` blob binding those keys to the
    identity id as principal. Any lookup problem (unknown id, corrupt store, I/O
    error) or a non-identity / keyless ticket yields ``None`` — this function
    never raises.
    """
    # Import lazily to avoid an import cycle (``rebar`` imports the attest package).
    import rebar

    try:
        ticket = rebar.show_ticket(identity_id, repo_root=repo_root)
    except Exception:  # noqa: BLE001 — any lookup failure (unknown id, corrupt store, I/O) → no trust root, never raise
        return None
    if ticket is None:
        return None
    if ticket.get("ticket_type") != "identity":
        return None
    keys = cast("list[str]", ticket.get("keys") or [])
    if not keys:
        return None
    return allowed_signers_from_keys(keys, principal=identity_id)


def keyop_payload(op: str, identity_id: str, public_key: str) -> bytes:
    """The canonical bytes a KEY-op signature covers (epic gnu-whale-ichor / e165).

    ``op`` is ``"KEY_ADD"`` / ``"KEY_REVOKE"``. The payload binds the operation to a
    specific identity AND public key, so a signature over one key-op can never be
    replayed as authorization for a different op / identity / key. Canonical (sorted-key,
    compact) so signer and verifier derive byte-identical bytes independent of dict order.
    """
    from rebar._store.canonical import canonical_str

    return canonical_str({"op": op, "identity_id": identity_id, "public_key": public_key}).encode(
        "utf-8"
    )


def _keyring_for(identity_id: str, *, repo_root=None) -> list:
    """The identity's position-based ``keyring`` records, or ``[]`` on ANY lookup problem
    (unknown id, corrupt store, non-identity, I/O). Never raises — mirrors
    :func:`resolve_trust_root`'s fail-closed contract."""
    import rebar

    try:
        ticket = rebar.show_ticket(identity_id, repo_root=repo_root)
    except Exception:  # noqa: BLE001 — any lookup failure → no keyring, never raise
        return []
    if not isinstance(ticket, dict):
        return []
    ring = ticket.get("keyring")
    return ring if isinstance(ring, list) else []


def resolve_event_commit(position: str, ticket_dir: str, *, repo_root=None) -> str | None:
    """The tickets-branch commit SHA that INTRODUCED the event file with ``position``
    prefix, or ``None`` (epic gnu-whale-ichor — the git-commit-ancestry anchor).

    ``position`` is an event's ``{timestamp}-{uuid}`` filename prefix; the event TYPE is
    NOT an input, so we glob the prefix (``<position>-*.json``) under ``ticket_dir`` and ask
    ``git log --diff-filter=A --full-history`` for the commit that added it. ``--full-history``
    disables history simplification so the glob sees EVERY add — including a compacted event
    whose original add commit is off the first-parent chain (without it the query is
    topology-dependent and can miss the real introducing commit, matching
    :func:`build_introducing_commit_map`'s walk). The LAST line of the log (the oldest = the
    add commit) is returned. Any failure — git non-zero, no match, git missing, a timeout, or
    any exception — yields ``None``; this function NEVER raises (fail-closed, mirroring
    :func:`resolve_trust_root`)."""
    if not position or not ticket_dir:
        return None
    try:
        from rebar._commands._seam import tracker_dir

        tracker = str(tracker_dir(repo_root))
        rel = os.path.relpath(ticket_dir, tracker)
        pathspec = f"{rel}/{position}-*.json"
        proc = subprocess.run(
            [
                "git",
                "-C",
                tracker,
                "log",
                "--diff-filter=A",
                "--full-history",
                "--format=%H",
                "--",
                pathspec,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if proc.returncode != 0:
            return None
        lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
        return lines[-1] if lines else None
    except Exception:  # noqa: BLE001 — ANY git/lookup failure → no commit, never raise (fail-closed)
        return None


def resolve_position_commit(position: str, tracker: str, *, repo_root=None) -> str | None:
    """The tickets-branch commit SHA that INTRODUCED the event file with ``position`` prefix,
    searched GLOBALLY across ``tracker`` (a sibling of :func:`resolve_event_commit` for callers
    that do not know the owning ticket dir — e.g. the op-cert era anchor, whose boundary positions
    are recorded out-of-band and are not scoped to one ticket).

    ``position`` is an event's ``{timestamp}-{uuid}`` prefix; positions are GLOBALLY unique, so a
    ``*{position}-*.json`` pathspec matches exactly the one introducing file wherever it lives. As
    with :func:`resolve_event_commit`, the query uses ``--full-history`` so the broad pathspec sees
    every add regardless of topology (a compacted event's add commit is often off the first-parent
    chain), and the LAST line of ``git log --diff-filter=A --full-history`` (the oldest = the add
    commit) is returned; ANY failure — git non-zero, no match, git missing, a timeout, or any
    exception — yields ``None`` (this function NEVER raises, fail-closed)."""
    if not position or not tracker:
        return None
    try:
        pathspec = f"*{position}-*.json"
        proc = subprocess.run(
            [
                "git",
                "-C",
                tracker,
                "log",
                "--diff-filter=A",
                "--full-history",
                "--format=%H",
                "--",
                pathspec,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if proc.returncode != 0:
            return None
        lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
        return lines[-1] if lines else None
    except Exception:  # noqa: BLE001 — ANY git/lookup failure → no commit, never raise (fail-closed)
        return None


def keys_valid_at_anchor(
    records: list[dict],
    anchor_commit: str,
    anchor_position: str | None,
    *,
    resolve,
    is_ancestor,
) -> list[str]:
    """The shared ancestry + intra-commit-position era predicate (epic gnu-whale-ichor).

    Given keyring ``records`` (dicts carrying ``public_key`` plus ``added_at`` / ``revoked_at``
    POSITION strings), return the public keys VALID at the anchor ``(anchor_commit,
    anchor_position)``. ``resolve(position) -> commit | None`` maps a log position to its
    introducing tickets-branch commit; ``is_ancestor(ancestor, descendant) -> bool`` decides
    ancestry on the tickets branch. A key is VALID iff its add-commit is an ANCESTOR of
    ``anchor_commit`` AND (it is not revoked OR its revoke-commit is NOT an ancestor). When an
    add/revoke commit EQUALS ``anchor_commit`` and ``anchor_position`` is given, the two are
    ordered by POSITION instead (a total order within one commit): added counts iff
    ``added_at <= anchor_position``; revoked counts iff ``revoked_at <= anchor_position``.

    This is the SINGLE era rule; both :func:`verify_authorship_at_commit` and
    :func:`rebar.attest.opcert.verify_opcert` call it with their own resolvers, so the rule is
    never duplicated. Pure: the caller owns fail-closed error handling around ``resolve`` /
    ``is_ancestor``."""
    valid_keys: list[str] = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        pub = rec.get("public_key")
        added_at = rec.get("added_at")
        revoked_at = rec.get("revoked_at")
        if not isinstance(pub, str) or not pub:
            continue
        if not isinstance(added_at, str) or not added_at:
            continue
        added_commit = resolve(added_at)
        if added_commit is None:
            continue
        if added_commit == anchor_commit and anchor_position is not None:
            added = added_at <= anchor_position
        else:
            added = is_ancestor(added_commit, anchor_commit)
        if not added:
            continue
        revoked = False
        if isinstance(revoked_at, str) and revoked_at:
            revoked_commit = resolve(revoked_at)
            if revoked_commit is not None:
                if revoked_commit == anchor_commit and anchor_position is not None:
                    revoked = revoked_at <= anchor_position
                else:
                    revoked = is_ancestor(revoked_commit, anchor_commit)
        if revoked:
            continue
        valid_keys.append(pub)
    return valid_keys


def build_introducing_commit_map(*, repo_root=None) -> dict[str, str]:
    """Map every tracker event-file path (relative to the tracker root) to the OLDEST commit
    that ADDED it, resolved in a SINGLE ``git log`` pass — the batched form of
    :func:`resolve_event_commit`.

    :func:`resolve_event_commit` runs one full-history ``git log`` per event; the merge-gate
    calls it once per in-scope event, so a whole-store scan is O(events × history) (bug 1cc0 —
    ~1.8 h at real store scale). This walks history ONCE (O(events + history), ~2 s) and returns
    a lookup table, so the gate resolves every event's introducing commit without a per-event
    subprocess. Callers keep :func:`resolve_event_commit` as a fail-closed fallback for any path
    absent from the map (e.g. a file introduced only inside a merge commit — see below).

    Mechanics (validated against the per-event resolver — 0 mismatches over 700 sampled events):

    * ``--diff-filter=A --name-only`` lists the paths ADDED by each commit;
    * ``--full-history`` disables history simplification, so a broad ``*.json`` pathspec sees
      every add that a per-single-path query would (the walk that makes the two agree);
    * ``--no-renames`` keeps a rename as add-at-new-path (events are immutable and never
      renamed, so this only guards against spurious rename detection);
    * ``--no-merges`` skips merge commits — ``--name-only`` shows no files for a merge anyway,
      and a ticket event's real creating commit is always a non-merge on the tickets branch;
    * ``%x1e`` (ASCII record separator) prefixes each hash so records parse unambiguously
      without ``-z`` framing — the event-path charset (``<hex>/<ts>-<uuid>-<TYPE>.json``) never
      needs quoting, and ``0x1e`` can appear in neither a path nor a SHA (Hugo's ``gitmap``
      uses the same separator technique);
    * ``-c log.showSignature=false`` avoids ``gpg:`` lines corrupting the stream (and the ~3×
      slowdown) when a host has signature display on.

    Git emits newest→oldest, so overwriting each path's entry as we stream leaves the OLDEST
    add winning — matching :func:`resolve_event_commit`'s ``lines[-1]``. Never raises: any git
    failure yields ``{}`` and every caller falls back to the per-event resolver (fail-closed).
    """
    try:
        from rebar._commands._seam import tracker_dir

        tracker = str(tracker_dir(repo_root))
        proc = subprocess.run(
            [
                "git",
                "-c",
                "log.showSignature=false",
                "-C",
                tracker,
                "log",
                "--diff-filter=A",
                "--full-history",
                "--no-merges",
                "--no-renames",
                "--format=%x1e%H",
                "--name-only",
                "--",
                "*.json",
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if proc.returncode != 0:
            return {}
        commit_map: dict[str, str] = {}
        for record in proc.stdout.split("\x1e"):
            if not record.strip():
                continue
            lines = record.split("\n")
            sha = lines[0].strip()
            if len(sha) != 40:
                continue
            for path in lines[1:]:
                path = path.strip()
                if path:
                    # newest→oldest walk; overwrite so the OLDEST add wins (matches
                    # resolve_event_commit's lines[-1]).
                    commit_map[path] = sha
        return commit_map
    except Exception:  # noqa: BLE001 — ANY git/lookup failure → empty map, never raise (fail-closed)
        return {}


def build_position_commit_map(*, repo_root=None) -> dict[str, str]:
    """Map every event POSITION (``{timestamp}-{uuid}`` prefix) to the OLDEST commit that ADDED
    its ``*.json`` file, resolved in a SINGLE ``git log`` pass — the position-keyed sibling of
    :func:`build_introducing_commit_map`.

    The merge-gate re-resolves a compacted LEDGER entry's null ``commit_sha`` from its recorded
    ``position``; doing that per-entry via :func:`resolve_position_commit` is one full-history
    ``git log`` per entry (O(entries × history) — the whole-store gate's dominant cost, ~2 s
    each). This walks history ONCE (exactly like :func:`build_introducing_commit_map`) and
    returns a position→commit lookup, so the gate resolves every ledger entry with an O(1) map
    hit and only falls back to :func:`resolve_position_commit` for a position the map misses
    (fail-closed).

    Keying: an event file is named ``{timestamp}-{uuid}-{TYPE}.json`` and its position is the
    ``{timestamp}-{uuid}`` prefix; since an event TYPE is always a dash-free ``UPPER_SNAKE`` name,
    the position is the basename with ``.json`` stripped and the trailing ``-{TYPE}`` removed
    (``rsplit('-', 1)[0]``) — the exact inverse of :func:`resolve_position_commit`'s
    ``*{position}-*.json`` pathspec. The same ``--full-history --no-merges --no-renames`` flags
    as :func:`build_introducing_commit_map` are used so a broad pathspec sees EVERY add (including
    a compacted event whose add commit is off the first-parent chain). Git emits newest→oldest, so
    overwriting each position's entry as we stream leaves the OLDEST add winning — matching
    :func:`resolve_position_commit`'s ``lines[-1]``. Never raises: any git failure yields ``{}``
    and the caller falls back to the per-entry resolver (fail-closed).
    """
    try:
        from rebar._commands._seam import tracker_dir

        tracker = str(tracker_dir(repo_root))
        proc = subprocess.run(
            [
                "git",
                "-c",
                "log.showSignature=false",
                "-C",
                tracker,
                "log",
                "--diff-filter=A",
                "--full-history",
                "--no-merges",
                "--no-renames",
                "--format=%x1e%H",
                "--name-only",
                "--",
                "*.json",
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if proc.returncode != 0:
            return {}
        position_map: dict[str, str] = {}
        for record in proc.stdout.split("\x1e"):
            if not record.strip():
                continue
            lines = record.split("\n")
            sha = lines[0].strip()
            if len(sha) != 40:
                continue
            for path in lines[1:]:
                path = path.strip()
                if not path or not path.endswith(".json"):
                    continue
                # basename is ``{position}-{TYPE}.json``; TYPE is dash-free, so stripping ``.json``
                # and the trailing ``-{TYPE}`` recovers the ``{timestamp}-{uuid}`` position.
                base = os.path.basename(path)[: -len(".json")]
                position = base.rsplit("-", 1)[0] if "-" in base else base
                if position:
                    # newest→oldest walk; overwrite so the OLDEST add wins (matches
                    # resolve_position_commit's lines[-1]).
                    position_map[position] = sha
        return position_map
    except Exception:  # noqa: BLE001 — ANY git/lookup failure → empty map, never raise (fail-closed)
        return {}


def verify_authorship_at_commit(
    envelope: dsse.Envelope,
    identity_id: str,
    event_commit: str,
    event_position: str | None,
    *,
    repo_root=None,
) -> registry.Verdict:
    """Verify ``envelope`` against ONLY the keys valid for the event at ``event_commit``
    (epic gnu-whale-ichor — the git-commit-ancestry validity model).

    Unlike :func:`verify_authorship` (which trusts the identity's CURRENTLY-valid keys), the
    trust root here is built from exactly the keyring records that were live as of
    ``event_commit``. For each record the ``added_at`` / ``revoked_at`` POSITIONS are
    resolved to commits via :func:`resolve_event_commit`; a key is VALID iff its add-commit
    is an ANCESTOR of ``event_commit`` AND (its revoke-commit is ``None`` OR is NOT an
    ancestor of ``event_commit``). Ancestry is decided by ``git merge-base --is-ancestor``.

    Intra-commit refinement: ``merge-base --is-ancestor(C, C)`` is true, so when a key's
    add/revoke commit EQUALS ``event_commit`` and ``event_position`` is given, the two are
    ordered by POSITION instead (a total order within one commit): added-in-same-commit
    counts as added iff ``added_at <= event_position``; revoked-in-same-commit counts as
    revoked iff ``revoked_at <= event_position``.

    An empty valid-key trust root yields a non-verified ``"unknown_principal"`` Verdict.
    ANY git subprocess failure / timeout / exception also yields a non-verified Verdict —
    this function NEVER raises for a git/lookup problem (fail-closed)."""
    try:
        from rebar._commands._seam import tracker_dir

        tracker = str(tracker_dir(repo_root))
        ticket_dir = os.path.join(tracker, identity_id)

        def _is_ancestor(ancestor: str, descendant: str) -> bool:
            proc = subprocess.run(
                ["git", "-C", tracker, "merge-base", "--is-ancestor", ancestor, descendant],
                capture_output=True,
                text=True,
                timeout=30,
            )
            return proc.returncode == 0

        def _resolve(position: str) -> str | None:
            return resolve_event_commit(position, ticket_dir, repo_root=repo_root)

        # The keyring records already carry ``added_at`` / ``revoked_at`` position strings, which
        # is exactly the shape ``keys_valid_at_anchor`` consumes — so we pass them straight through
        # to the SHARED era predicate (identical to the op-cert gate's, never duplicated).
        valid_keys = keys_valid_at_anchor(
            _keyring_for(identity_id, repo_root=repo_root),
            event_commit,
            event_position,
            resolve=_resolve,
            is_ancestor=_is_ancestor,
        )
    except Exception:  # noqa: BLE001 — ANY git/lookup failure → non-verified, never raise (fail-closed)
        return registry.Verdict(
            verified=False,
            verdict="unknown_principal",
            reason=(
                f"authorship verification for identity {identity_id!r} failed (git/lookup error)"
            ),
        )

    if not valid_keys:
        return registry.Verdict(
            verified=False,
            verdict="unknown_principal",
            reason=(
                f"no keys valid at commit {event_commit!r} for identity {identity_id!r} "
                "(key not yet added, already revoked, or identity unknown/keyless)"
            ),
        )
    trust_root = allowed_signers_from_keys(valid_keys, principal=identity_id)
    return registry.verify(AUTHORSHIP_KIND, envelope, trust_root)


def verify_authorship(
    envelope: dsse.Envelope, identity_id: str, *, repo_root=None
) -> registry.Verdict:
    """Verify ``envelope`` as an authorship attestation by ``identity_id``.

    The trust root is resolved from the author identity's own in-band keys; if
    it cannot be resolved (unknown / keyless identity, or any lookup error) the
    result is a non-verified ``Verdict`` with verdict ``"unknown_principal"``.
    This function never raises for an identity-lookup problem.
    """
    trust_root = resolve_trust_root(identity_id, repo_root=repo_root)
    if trust_root is None:
        return registry.Verdict(
            verified=False,
            verdict="unknown_principal",
            reason=(
                f"no verifiable authorship trust root for identity {identity_id!r} "
                "(unknown identity, not an identity ticket, or no keys recorded)"
            ),
        )
    return registry.verify(AUTHORSHIP_KIND, envelope, trust_root)


def verify_authorship_any_key(
    envelope: dsse.Envelope, identity_id: str, *, repo_root=None
) -> registry.Verdict:
    """Verify ``envelope`` against ANY key the identity has EVER held (epic gnu-whale-ichor).

    The trust root is built from EVERY keyring record's public key — regardless of
    ``revoked_at`` — so this answers "was this signed by a real key of this identity at all?",
    independent of era validity. Used by the merge-gate to distinguish a forged signature
    (``bad-signature``) from a real-but-wrong-era one (``key_not_valid_at_era``). An empty
    keyring / any lookup failure yields a non-verified ``Verdict``. Never raises.
    """
    keys: list[str] = []
    for rec in _keyring_for(identity_id, repo_root=repo_root):
        if not isinstance(rec, dict):
            continue
        pub = rec.get("public_key")
        if isinstance(pub, str) and pub:
            keys.append(pub)
    if not keys:
        return registry.Verdict(
            verified=False,
            verdict="unknown_principal",
            reason=(
                f"no keys recorded for identity {identity_id!r} "
                "(unknown identity, not an identity ticket, or empty keyring)"
            ),
        )
    trust_root = allowed_signers_from_keys(keys, principal=identity_id)
    return registry.verify(AUTHORSHIP_KIND, envelope, trust_root)


def identify_signer(envelope: dsse.Envelope, identity_id: str, *, repo_root=None) -> str | None:
    """Return the FIRST keyring public key (incl. revoked) whose single-key trust root
    verifies ``envelope``, or ``None`` (epic gnu-whale-ichor / 117b).

    Iterates the identity's keyring in order, building a ONE-key trust root per record and
    running :func:`registry.verify`; the first key whose lone verify passes is returned. No
    match (forged / foreign signature) or any lookup failure yields ``None``. Never raises.
    """
    for rec in _keyring_for(identity_id, repo_root=repo_root):
        if not isinstance(rec, dict):
            continue
        pub = rec.get("public_key")
        if not isinstance(pub, str) or not pub:
            continue
        trust_root = allowed_signers_from_keys([pub], principal=identity_id)
        try:
            verdict = registry.verify(AUTHORSHIP_KIND, envelope, trust_root)
        except Exception:  # noqa: BLE001 — a verify hiccup on one key → not this key, never raise
            continue
        if verdict.verified:
            return pub
    return None


def authorship_content_hash(event: dict) -> str:
    """The SHA-256 (lowercase hex) digest over the event's canonical JSON bytes,
    with the ``author_sig`` key EXCLUDED (epic gnu-whale-ichor / c96d).

    Signer and verifier derive byte-identical bytes via the repo's canonical
    serializer (sorted-key, compact), so the digest binds an event's content
    independent of dict order and independent of any signature carried on it.
    """
    import hashlib

    from rebar._store.canonical import canonical_str

    payload = canonical_str({k: v for k, v in event.items() if k != "author_sig"}).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def build_authorship_statement(event_uuid: str, content_hash: str) -> dict:
    """The in-toto Statement an authorship signature wraps (epic gnu-whale-ichor / c96d).

    The single subject binds the event's ``uuid`` (as ``name``) to its content
    digest (``sha256``); the ``predicateType`` is the pinned authorship payload
    type and the ``predicate`` is empty (the binding lives entirely in ``subject``).
    """
    return {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": [{"name": event_uuid, "digest": {"sha256": content_hash}}],
        "predicateType": PAYLOAD_TYPE,
        "predicate": {},
    }


def sign_event_authorship(event: dict, key_path: str, principal: str) -> dsse.Envelope:
    """Sign ``event``'s authorship as an in-toto Statement (epic gnu-whale-ichor / c96d).

    Wraps ``{event_uuid, content_hash}`` in an in-toto Statement, serializes it to
    canonical JSON, and delegates to the low-level :func:`sign_authorship` primitive
    (which DSSE-envelopes and signs those bytes under the pinned authorship
    namespace). The Statement JSON is the DSSE payload.
    """
    from rebar._store.canonical import canonical_str

    content_hash = authorship_content_hash(event)
    statement = build_authorship_statement(event["uuid"], content_hash)
    payload = canonical_str(statement).encode("utf-8")
    return sign_authorship(payload, key_path, principal)


def verify_event_authorship(
    event: dict, envelope: dsse.Envelope, identity_id: str, *, repo_root=None
) -> registry.Verdict:
    """Verify ``envelope`` is a valid authorship Statement over ``event`` by ``identity_id``.

    Three fail-closed gates: (1) the DSSE payload parses as an in-toto Statement;
    (2) its subject binds this event's ``uuid`` AND content hash; (3) the DSSE
    signature verifies against the author identity's in-band keys (delegated to
    :func:`verify_authorship`). Any parse/shape problem yields a non-verified
    ``Verdict`` — this function never raises for a lookup/parse problem.
    """
    import json

    try:
        statement = json.loads(envelope.payload.decode("utf-8"))
        subject = statement["subject"]
        if not isinstance(subject, list) or not subject:
            raise ValueError("empty or non-list subject")
        first = subject[0]
        subject_name = first["name"]
        subject_hash = first["digest"]["sha256"]
    except Exception:  # noqa: BLE001 — malformed / non-Statement payload → non-verified, never raise
        return registry.Verdict(
            verified=False,
            verdict="malformed_statement",
            reason="envelope payload is not a valid in-toto authorship Statement",
        )

    if subject_name != event["uuid"] or subject_hash != authorship_content_hash(event):
        return registry.Verdict(
            verified=False,
            verdict="subject_mismatch",
            reason="Statement subject does not bind this event's uuid and content hash",
        )

    return verify_authorship(envelope, identity_id, repo_root=repo_root)
