"""Per-``env_id`` authorship health for ``fsck`` (bug ed5c-42fc-bb7f-4cf4).

``fsck`` already emits a store-wide advisory ``authorship: N signed, M unsigned`` line. That
tally is blind to the failure it most needs to catch: ONE writer (one clone, one bot, one
container) whose identity stops resolving, so every event it appends is unsigned while every
OTHER writer keeps signing. Summed store-wide, the healthy writers' volume swallows the broken
one — which is exactly how a review bot wrote ~8900 unsigned, ``Unknown``-authored events for a
month before an unrelated audit noticed (bug beb1). The per-env signed-rate breakdown that
finally found it was a one-off script; this module makes it a standing, COUNTED check.

The rule, and why it is gated
-----------------------------
An env is reported when it has at least one event AND either **no** event of its is signed, or
**every** event of its is authored ``Unknown``/absent — the two signatures of a writer with no
usable identity.

Applied naively that would fire on every store with history, because all events written before
authorship signing existed are unsigned, permanently and unfixably. So the check is gated on
signing ADOPTION: it stays silent unless the store contains at least one signed event, and it
only reports an env whose most recent event is at or after the store's EARLIEST signed one.
An env that went dormant before signing arrived is history, not a defect; an env still writing
while its peers sign is the live problem. Measured against the tracker this was written from
(18 envs), the gate suppresses all four dormant 0%-signed envs and would still have reported
beb1's env, which kept writing unsigned alongside a signing peer.

Presence only — like the store-wide line, this counts the PRESENCE of ``author_sig`` and never
verifies a signature. Cryptographic verification is ``rebar verify-authorship``.
"""

from __future__ import annotations

# Snapshots are derived projections, not appended events: a SNAPSHOT carries no independent
# authorship of its own, so folding it into a writer's tally would double-count the events it
# compacted. ``_scan``'s Check 1 walks every ``*.json`` in the ticket dir, so the filter lives
# here rather than at the call site.
_SNAPSHOT_SUFFIX = "-SNAPSHOT.json"

# An event whose ``author`` is absent, empty, or the literal placeholder is "unattributed" for
# this check. ``Unknown`` is what the reducer projects when no author was ever stamped.
_UNKNOWN_AUTHORS = frozenset({"", "unknown"})


class _EnvRow:
    """Mutable per-env accumulator (events, signed, unattributed, latest timestamp)."""

    __slots__ = ("events", "last_ts", "signed", "unattributed")

    def __init__(self) -> None:
        self.events = 0
        self.signed = 0
        self.unattributed = 0
        self.last_ts = 0


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

        ts = payload.get("timestamp")
        ts = ts if isinstance(ts, int) else 0
        row.last_ts = max(row.last_ts, ts)

        if payload.get("author_sig"):
            row.signed += 1
            if self._first_signed_ts is None or ts < self._first_signed_ts:
                self._first_signed_ts = ts

        author = payload.get("author")
        if not isinstance(author, str) or author.strip().lower() in _UNKNOWN_AUTHORS:
            row.unattributed += 1

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
                "clone, then `rebar verify-authorship`."
            )
        return out
