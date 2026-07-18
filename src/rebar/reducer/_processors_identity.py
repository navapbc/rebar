"""Identity, keyring, and attestation processors for the ticket reducer.

Leaf extracted from ``_processors.py`` (module-size split, epic
yestern-choral-mustang): the present-only author-attribution helper, the
position-based keyring folding (``KEY_ADD`` / ``KEY_REVOKE`` + genesis seeding),
and SIGNATURE-event attestation handling. This module imports nothing from
``_processors``, so the split stays one-way; ``_processors`` re-exports every
symbol here for back-compat. Behaviour is byte-for-byte unchanged.
"""

from __future__ import annotations


def _fold_author_attribution(target: dict, event: dict) -> None:
    """Surface denormalized author attribution PRESENT-ONLY (epic gnu-whale-ichor).

    Copies ``author_email`` / ``author_id`` from the event ENVELOPE onto ``target``
    only when the event carries them — so a pre-change event (no such keys) reduces to
    byte-identical state and no new keys appear anywhere. Mirrors the ``source_*``
    present-only handling. ``target`` is top-level state for a CREATE, or a per-entry
    record (comment / revert / signature) for the other stamping sites.
    """
    for _key in ("author_email", "author_id"):
        _val = event.get(_key)
        if _val is not None:
            target[_key] = _val


def _rederive_keyring_keys(state: dict) -> None:
    """Re-derive ``state['keys']`` from the keyring so existing ``keys`` readers
    (authorship trust root, show) keep working: the CURRENTLY-valid keys are the public
    keys of records with ``revoked_at is None``. Preserves keyring order; skips malformed
    records defensively (epic gnu-whale-ichor — position-based keyring)."""
    keys: list[str] = []
    for rec in state.get("keyring") or []:
        if not isinstance(rec, dict):
            continue
        pub = rec.get("public_key")
        if isinstance(pub, str) and pub and rec.get("revoked_at") is None:
            keys.append(pub)
    state["keys"] = keys


def process_key_event(state: dict, event: dict, data: dict, event_type: str) -> None:
    """Apply a KEY_ADD / KEY_REVOKE event to an identity's POSITION-based keyring
    (epic gnu-whale-ichor — the git-commit-ancestry validity model).

    A keyring record is ``{public_key, added_at: <position>, revoked_at: <position|None>}``
    where a POSITION is the event's ``{timestamp}-{uuid}`` filename prefix — the immutable
    anchor a verifier later resolves to the introducing tickets-branch commit. There is NO
    epoch cursor: the event's own position IS the ordinal.

    * ``KEY_ADD``  — append ``{public_key, added_at: <this event's position>, revoked_at:
      None}``.
    * ``KEY_REVOKE`` — set ``revoked_at = <this event's position>`` on the first matching
      STILL-VALID record (``public_key`` matches and ``revoked_at is None``); a revoke naming
      an unknown / already-revoked key folds no change.

    After folding, ``state['keys']`` is re-derived from the currently-valid records so
    downstream ``keys`` consumers are unaffected by the keyring representation.
    """
    public_key = data.get("public_key")
    keyring = state.setdefault("keyring", [])
    position = f"{event.get('timestamp')}-{event.get('uuid')}"
    if event_type == "KEY_ADD":
        if isinstance(public_key, str) and public_key:
            keyring.append({"public_key": public_key, "added_at": position, "revoked_at": None})
    elif event_type == "KEY_REVOKE":
        if isinstance(public_key, str) and public_key:
            for rec in keyring:
                if (
                    isinstance(rec, dict)
                    and rec.get("public_key") == public_key
                    and rec.get("revoked_at") is None
                ):
                    rec["revoked_at"] = position
                    break
    _rederive_keyring_keys(state)


def _bootstrap_genesis_keyring(state: dict, create_position: str) -> None:
    """Seed an ``identity``'s keyring from a static ``keys`` list carried on its CREATE
    (epic gnu-whale-ichor). Every genesis key is recorded as added at ``create_position``
    (the CREATE event's ``{timestamp}-{uuid}`` prefix), so a genesis key's add-commit
    resolves to the CREATE commit — NOT a magic sentinel. An identity created with NO keys
    keeps ``keyring=[]`` (its first KEY_ADD is then the TOFU add), so both pre-existing
    (keys-at-create) and new identities converge on the same position model."""
    keys = state.get("keys")
    if state.get("ticket_type") != "identity" or not isinstance(keys, list) or not keys:
        return
    ring = [
        {"public_key": k, "added_at": create_position, "revoked_at": None}
        for k in keys
        if isinstance(k, str) and k
    ]
    if ring:
        state["keyring"] = ring


def attestation_kind(manifest: list | None, data: dict) -> str | None:
    """Derive the attestation kind used to key ``state['attestations']``.

    The SIGNED ``manifest[0]`` is authoritative: the kind is the substring before the
    first ``":"`` (e.g. ``"plan-review: PASS"`` -> ``"plan-review"``,
    ``"completion-verifier: PASS"`` -> ``"completion-verifier"``). ``data['kind']`` is an
    UNSIGNED routing hint — it is never allowed to override the signed manifest, so a
    mismatched hint is ignored and the manifest-derived kind is used. Returns None for a
    blank/retired or otherwise unkindable manifest (no first line, or no ``":"``); such an
    event stays OUT of the map (it cannot key a kind)."""
    if not manifest:
        return None
    first = str(manifest[0])
    if ":" not in first:
        return None
    derived = first.split(":", 1)[0].strip() or None
    if derived is None:
        return None
    # ``data['kind']`` is an UNSIGNED routing hint. The signed manifest is authoritative, so
    # we consult the hint only to honor/validate it: a hint that disagrees with the
    # manifest-derived kind is IGNORED (the manifest wins) — a forged/buggy envelope kind can
    # never misroute a signed attestation. Either way the manifest-derived kind is returned.
    hint = data.get("kind")
    if hint is not None and str(hint).strip() != derived:
        return derived
    return derived


def process_signature(state: dict, event: dict, data: dict) -> None:
    """Apply a SIGNATURE event: maintain the most-recent ``state['signature']`` mirror
    AND, additively, file the record under its kind in ``state['attestations']``.

    The MIRROR keeps the exact prior single-slot last-writer-wins behavior — EVERY event
    (including a blank/retired one) replaces ``state['signature']`` — so the existing
    ``state.get('signature')`` consumers (verify, the close gate, fsck) are unchanged by this
    slice, and the SNAPSHOT/rollback mirror is automatic (the compactor
    builds compiled_state via this reducer).

    The MAP (``state['attestations']``, epic dark-acme-lumen) is purely additive: a kindable
    event sets ``attestations[kind]`` (per-key last-writer-wins, so re-signing one kind
    replaces only that kind and the others survive — fixing the cross-kind clobber). A
    blank/retired/unkindable event is SKIPPED for the map (it cannot key a kind; its staleness
    is handled later by validity-on-read, not by clobbering). Kind comes from the signed
    ``manifest[0]`` (``data['kind']`` is only a validated hint). The signed-at timestamp falls
    back to the event timestamp for forward-compat records.
    """
    _manifest = data.get("manifest")
    # Coerce to a list: never persist a non-list truthy value (e.g. a dict) into reduced
    # state, which would leak a malformed shape into show/MCP output (fail closed).
    manifest = _manifest if isinstance(_manifest, list) else []
    kind = attestation_kind(manifest, data)
    record = {
        "manifest": manifest,
        "algorithm": data.get("algorithm"),
        "signature": data.get("signature"),
        "key_id": data.get("key_id"),
        "head_sha": data.get("head_sha"),
        "signed_at": data.get("signed_at") or event.get("timestamp"),
        "author": event.get("author"),
        # The resolved (manifest-authoritative) kind, so the record is self-describing —
        # esp. for the legacy mirror, which has no map-key context. None for a
        # blank/retired/unkindable event.
        "kind": kind,
    }
    # Denormalized author attribution (epic gnu-whale-ichor): present-only on the record.
    _fold_author_attribution(record, event)
    # Asymmetric op-cert fields (keystone e4df): folded PRESENT-ONLY, so an HMAC event (no
    # ``envelope`` in data) reduces to a byte-identical record and these keys never appear on it.
    if data.get("envelope") is not None:
        record["envelope"] = data["envelope"]
        record["material_fingerprint"] = data.get("material_fingerprint")
        record["merged_log_commit"] = data.get("merged_log_commit")
        # The DSSE principal (env_id) the op-cert was signed under (story 8d8e), so the shape-aware
        # verify wrapper can classify a foreign-environment cert without decoding the envelope.
        record["principal"] = data.get("principal")
    # Mirror: unchanged single-slot semantics (back-compat for existing consumers).
    state["signature"] = record
    # Map: additive, kind-keyed; skip blank/retired/unkindable events (no key derivable).
    if kind is not None:
        state.setdefault("attestations", {})[kind] = record
