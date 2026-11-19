"""Compute counted authorship health for each ``env_id``.

An environment is unhealthy when it has events and either none are signed or all authors are
missing or ``Unknown``. The check runs only after the store has adopted signing, and reports
only environments whose newest event is at least as recent as the earliest signed event. This
excludes dormant pre-signing writers while detecting active writers without usable identities.

The tally checks only whether ``author_sig`` is present. ``rebar verify-identity`` performs
cryptographic verification. It also records, per ``(env_id, signing key)``, the first and last
event timestamp, read from the public key embedded in each ``author_sig`` SSHSIG, for the
environment-identity check (:func:`rebar._store.env_identity.divergence_report`).
"""

from __future__ import annotations

import base64
import hashlib

from rebar._store.env_identity import Span
from rebar.attest import dsse
from rebar.attest.sshsig import embedded_public_key

# Snapshots are derived projections, not appended events: a SNAPSHOT carries no independent
# authorship of its own, so folding it into a writer's tally would double-count the events it
# compacted. ``_scan``'s Check 1 walks every ``*.json`` in the ticket dir, so the filter lives
# here rather than at the call site.
_SNAPSHOT_SUFFIX = "-SNAPSHOT.json"

# An event whose ``author`` is absent, empty, or the literal placeholder is "unattributed" for
# this check. ``Unknown`` is what the reducer projects when no author was ever stamped.
_UNKNOWN_AUTHORS = frozenset({"", "unknown"})


def _widen(span: Span | None, ts: int) -> Span:
    """``span`` extended to cover ``ts``."""
    if span is None:
        return (ts, ts)
    return (min(span[0], ts), max(span[1], ts))


def signer_key_id(author_sig: object) -> str | None:
    """The ``SHA256:`` fingerprint (as ``ssh-keygen -l`` prints it) of the key that made
    ``author_sig``, or ``None`` when the envelope or its SSHSIG cannot be parsed.

    The key is the public half embedded in the SSHSIG; this establishes which key signed,
    not that the signature verifies (that is ``rebar verify-identity``'s job)."""
    if not isinstance(author_sig, str):
        return None
    try:
        signatures = dsse.decode(author_sig).signatures
    except (ValueError, KeyError, TypeError):
        return None
    if not signatures:
        return None
    public_key = embedded_public_key(signatures[0].sig)
    if public_key is None:
        return None
    blob = base64.b64decode(public_key.split(" ", 1)[1])
    digest = base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii")
    return "SHA256:" + digest.rstrip("=")


class _EnvRow:
    """Mutable per-env accumulator (events, signed, unattributed, latest timestamp, and each
    signing key's time span)."""

    __slots__ = ("events", "keys", "last_ts", "signed", "unattributed")

    def __init__(self) -> None:
        self.events = 0
        self.signed = 0
        self.unattributed = 0
        self.last_ts = 0
        self.keys: dict[str, Span] = {}


class EnvAuthorshipTally:
    """Accumulates per-``env_id`` authorship presence, then reports the silent writers.

    Fed one already-parsed event at a time by ``fsck._scan`` so the check costs NO extra
    filesystem pass — Check 1 parses every event file for JSON validity and discards the
    payload; this simply observes it on the way past.
    """

    def __init__(self) -> None:
        self._envs: dict[str, _EnvRow] = {}
        self._first_signed_ts: int | None = None

    def observe(self, filename: str, payload: object) -> None:
        """Record one event file. Anything that is not an attributable event — a snapshot, a
        non-dict payload, an event with no ``env_id`` — is ignored rather than guessed at: a
        writer we cannot name is a writer we cannot report."""
        if filename.endswith(_SNAPSHOT_SUFFIX):
            return
        if not isinstance(payload, dict):
            return
        env_id = payload.get("env_id")
        if not isinstance(env_id, str) or not env_id:
            return

        row = self._envs.get(env_id)
        if row is None:
            row = self._envs[env_id] = _EnvRow()
        row.events += 1

        raw_ts = payload.get("timestamp")
        ts = raw_ts if isinstance(raw_ts, int) else 0
        stamped = raw_ts if isinstance(raw_ts, int) and not isinstance(raw_ts, bool) else None
        row.last_ts = max(row.last_ts, ts)

        author_sig = payload.get("author_sig")
        if author_sig:
            row.signed += 1
            if self._first_signed_ts is None or ts < self._first_signed_ts:
                self._first_signed_ts = ts
            # An unstamped event cannot be placed in time, and an unparseable signature names
            # no key, so neither can create or hide an overlap.
            key = signer_key_id(author_sig) if stamped is not None else None
            if key is not None and stamped is not None:
                row.keys[key] = _widen(row.keys.get(key), stamped)

        author = payload.get("author")
        if not isinstance(author, str) or author.strip().lower() in _UNKNOWN_AUTHORS:
            row.unattributed += 1

    def key_spans(self) -> dict[tuple[str, str], Span]:
        """Return each ``(env_id, signing-key fingerprint)`` pair's event time span for the
        identity check (:func:`rebar._store.env_identity.divergence_report`)."""
        return {
            (env_id, key): span
            for env_id, row in self._envs.items()
            for key, span in row.keys.items()
        }

    def findings(self) -> list[str]:
        """``UNSIGNED_ENV:`` lines for every env that is silently writing unsigned.

        Empty (the common case) when the store has not adopted signing, or when every env that
        wrote after adoption signs. Sorted by ``env_id`` so the output is deterministic.
        """
        adopted = self._first_signed_ts
        if adopted is None:
            return []

        out: list[str] = []
        for env_id in sorted(self._envs):
            row = self._envs[env_id]
            if not row.events or row.last_ts < adopted:
                continue
            never_signed = row.signed == 0
            never_attributed = row.unattributed == row.events
            if not (never_signed or never_attributed):
                continue
            reasons = []
            if never_signed:
                reasons.append(f"0 of {row.events} event(s) signed")
            if never_attributed:
                reasons.append(f"all {row.events} event(s) authored 'Unknown'")
            out.append(
                f"UNSIGNED_ENV: {env_id} — {' and '.join(reasons)}, yet this env wrote after "
                "authorship signing was adopted in this store. Its writer has no usable "
                "identity: check `rebar identity use <id>` and identity.signing_key on that "
                "clone, then `rebar verify-identity`."
            )
        return out
