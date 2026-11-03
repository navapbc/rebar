"""Compute counted authorship health for each ``env_id``.

An environment is unhealthy when it has events and either none are signed or all authors are
missing or ``Unknown``. The check runs only after the store has adopted signing, and reports
only environments whose newest event is at least as recent as the earliest signed event. This
excludes dormant pre-signing writers while detecting active writers without usable identities.

The tally checks only whether ``author_sig`` is present. ``rebar verify-identity`` performs
cryptographic verification.
"""

from __future__ import annotations

from rebar._store.env_identity import Span

# Snapshots are derived projections, not appended events: a SNAPSHOT carries no independent
# authorship of its own, so folding it into a writer's tally would double-count the events it
# compacted. ``_scan``'s Check 1 walks every ``*.json`` in the ticket dir, so the filter lives
# here rather than at the call site.
_SNAPSHOT_SUFFIX = "-SNAPSHOT.json"

# An event whose ``author`` is absent, empty, or the literal placeholder is "unattributed" for
# this check. ``Unknown`` is what the reducer projects when no author was ever stamped.
_UNKNOWN_AUTHORS = frozenset({"", "unknown"})


def _widen(span: Span, ts: int | None) -> Span:
    """``span`` extended to cover ``ts``; an unstamped event leaves it unchanged."""
    if ts is None:
        return span
    if span is None:
        return (ts, ts)
    return (min(span[0], ts), max(span[1], ts))


class _EnvRow:
    """Mutable per-env accumulator (events, signed, unattributed, latest timestamp)."""

    __slots__ = ("authors", "events", "last_ts", "signed", "unattributed")

    def __init__(self) -> None:
        self.events = 0
        self.signed = 0
        self.unattributed = 0
        self.last_ts = 0
        self.authors: dict[str, Span] = {}


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

        if payload.get("author_sig"):
            row.signed += 1
            if self._first_signed_ts is None or ts < self._first_signed_ts:
                self._first_signed_ts = ts

        author = payload.get("author")
        if not isinstance(author, str) or author.strip().lower() in _UNKNOWN_AUTHORS:
            row.unattributed += 1
        elif author.strip():
            name = author.strip()
            row.authors[name] = _widen(row.authors.get(name), stamped)

    def identity_spans(self) -> dict[tuple[str, str], Span]:
        """Return each ``(env_id, author)`` pair's event time span for identity-divergence
        checks (:func:`rebar._store.env_identity.divergence_report`)."""
        return {
            (env_id, author): span
            for env_id, row in self._envs.items()
            for author, span in row.authors.items()
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
