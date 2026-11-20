"""Re-sign failing events during the reclaim collapse (story minimum-seedy-lowchen).

The reclaimed ``tickets`` history may only be published when enforced
``verify-identity --all`` passes on it with no grandfather boundary. Events that fail it
(unsigned, or signed by a key that is not on the author identity's keyring) are re-signed
during the rewrite with the identity key that was valid when each event was written.

:func:`build_resign_plan` does all of the work that can fail: it matches each failing
event to a raw event file or a SNAPSHOT ledger entry at the shadow's tip, chooses the era
key, signs through the ssh-agent at ``SSH_AUTH_SOCK`` and checks each signature. The
collapse then only substitutes the prepared bytes, so a signing failure never leaves a
half-written rewrite. Nothing here reads the configured live store: the keyring, the
position map and the ancestry oracle all come from the shadow tracker.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from rebar._store.canonical import canonical_bytes, canonical_str
from rebar._store.gitutil import run_git
from rebar.attest import authorship, dsse, registry

Signer = Callable[[bytes, str, str], dsse.Envelope]


class ResignError(RuntimeError):
    """The re-sign plan cannot be built or applied; nothing was written."""


@dataclass
class ResignPlan:
    """Prepared replacement bytes for one shadow tip.

    ``raw_blobs`` maps an original raw-event blob id to its re-signed bytes; event files are
    append-only, so one blob id names one event wherever it appears in history.
    ``ledger`` maps ``(event_uuid, content_hash)`` to ``(signature, signer_pubkey)``.
    ``substituted`` / ``resigned_ledger`` record what the collapse actually applied.
    """

    tip: str
    raw_blobs: dict[str, bytes]
    ledger: dict[tuple[str, str], tuple[str, str]]
    tip_raw_blobs: frozenset[str] = frozenset()
    substituted: set[str] = field(default_factory=set)
    resigned_ledger: set[tuple[str, str]] = field(default_factory=set)

    def raw_bytes(self, oid: str) -> bytes | None:
        data = self.raw_blobs.get(oid)
        if data is not None:
            self.substituted.add(oid)
        return data

    def resign_ledger(self, ledger: list) -> int:
        """Re-sign this plan's entries in a SNAPSHOT ledger in place; return the count."""
        changed = 0
        for entry in ledger:
            if not isinstance(entry, dict):
                continue
            key = (str(entry.get("event_uuid")), str(entry.get("content_hash")))
            replacement = self.ledger.get(key)
            if replacement is None:
                continue
            self.resigned_ledger.add(key)
            signature, signer = replacement
            if entry.get("signature") != signature or entry.get("signer_pubkey") != signer:
                entry["signature"] = signature
                entry["signer_pubkey"] = signer
                changed += 1
        return changed

    def assert_applied(self) -> None:
        missing_raw = self.tip_raw_blobs - self.substituted
        missing_ledger = set(self.ledger) - self.resigned_ledger
        if missing_raw or missing_ledger:
            raise ResignError(
                f"re-sign incomplete: {len(missing_raw)} raw event(s) and "
                f"{len(missing_ledger)} ledger entr(ies) were never rewritten"
            )


def build_resign_plan(
    shadow: str | os.PathLike[str],
    failing: Iterable[Mapping[str, object]],
    key_files: Mapping[str, str],
    *,
    identity_id: str,
    boundary: str,
    signer: Signer = authorship.sign_authorship,
) -> ResignPlan:
    """Match, key and sign every failing event at the shadow's tip.

    ``failing`` is the array enforced ``verify-identity --all --format json`` printed for
    this tip. ``key_files`` maps keyring public-key text to a public-key file whose private
    half the ssh-agent holds. Raises :class:`ResignError` before signing when an event is
    unmatched, has no era key, or names another author; and when a key is not in the agent.
    """
    root = Path(shadow).resolve()
    tip = _git(root, "rev-parse", "HEAD").strip()
    wanted = {str(f.get("event_uuid")): str(f.get("ticket_id")) for f in failing}
    raw, ledger_events = _match_targets(root, tip, wanted)
    ring = _Keyring(root, identity_id, boundary, key_files)
    ring.require_in_agent()

    choices: list[tuple[str, str, dict | None, tuple[str, str] | None]] = []
    keyless: list[str] = []
    for oid, (event, position) in raw.items():
        _check_author(event, identity_id)
        pub = ring.choose(position)
        if pub is None:
            keyless.append(str(event.get("uuid")))
        else:
            choices.append((oid, pub, event, None))
    for key, (position, recorded) in ledger_events.items():
        pub = ring.choose(position, fallback_commit=recorded)
        if pub is None:
            keyless.append(key[0])
        else:
            choices.append(("", pub, None, key))
    if keyless:
        raise ResignError(
            f"{len(keyless)} event(s) have no supplied identity key valid at their position: "
            + ", ".join(sorted(keyless)[:20])
        )

    plan = ResignPlan(tip=tip, raw_blobs={}, ledger={}, tip_raw_blobs=frozenset(raw))
    for oid, pub, raw_event, ledger_key in choices:
        if raw_event is not None:
            plan.raw_blobs[oid] = _resign_raw(raw_event, pub, ring, identity_id, signer)
        elif ledger_key is not None:
            sig = _sign(ledger_key[0], ledger_key[1], pub, ring, identity_id, signer)
            plan.ledger[ledger_key] = (sig, pub)
    return plan


# ── matching ────────────────────────────────────────────────────────────────


def _match_targets(
    root: Path, tip: str, wanted: Mapping[str, str]
) -> tuple[dict[str, tuple[dict, str]], dict[tuple[str, str], tuple[str, str | None]]]:
    from rebar._commands.reclaim_collapse_stream import ObjectReader

    reader = ObjectReader(root)
    try:
        return _scan_tip(root, tip, wanted, reader)
    finally:
        reader.close()


def _scan_tip(
    root: Path, tip: str, wanted: Mapping[str, str], reader
) -> tuple[dict[str, tuple[dict, str]], dict[tuple[str, str], tuple[str, str | None]]]:
    raw: dict[str, tuple[dict, str]] = {}
    snapshots: list[str] = []
    found: set[str] = set()
    tickets = set(wanted.values())
    for mode_oid, path in _ls_tree(root, tip, tickets):
        ticket, _, name = path.partition("/")
        if "/" in name or not name.endswith(".json") or name.startswith("."):
            continue
        if name.endswith("-SNAPSHOT.json"):
            snapshots.append(mode_oid)
            continue
        uuid = _uuid_of(name)
        if uuid in wanted and wanted[uuid] == ticket:
            event = json.loads(reader.read(mode_oid))
            raw[mode_oid] = (event, f"{event.get('timestamp')}-{event.get('uuid')}")
            found.add(uuid)
    ledger: dict[tuple[str, str], tuple[str, str | None]] = {}
    for oid in snapshots:
        snap = json.loads(reader.read(oid))
        entries = snap.get("data", {}).get("compiled_state", {}).get("authorship_ledger")
        for entry in entries if isinstance(entries, list) else []:
            uuid = str(entry.get("event_uuid")) if isinstance(entry, dict) else ""
            if uuid in wanted and uuid not in found:
                pos = entry.get("position") if isinstance(entry.get("position"), dict) else {}
                commit = pos.get("commit_sha") if isinstance(pos.get("commit_sha"), str) else None
                ledger[(uuid, str(entry.get("content_hash")))] = (
                    str(pos.get("position") or ""),
                    commit,
                )
    matched = found | {k[0] for k in ledger}
    unmatched = sorted(set(wanted) - matched)
    if unmatched:
        raise ResignError(
            f"{len(unmatched)} failing event(s) are neither a raw event nor a ledger entry "
            f"at {tip}: " + ", ".join(unmatched[:20])
        )
    return raw, ledger


def _uuid_of(name: str) -> str:
    """``<timestamp>-<uuid>-<TYPE>.json`` → ``<uuid>`` (36 characters after the timestamp)."""
    ts, _, rest = name.partition("-")
    return rest[:36] if ts.isdigit() else ""


def _check_author(event: dict, identity_id: str) -> None:
    author = event.get("author_id")
    if author not in (None, "", identity_id):
        raise ResignError(
            f"event {event.get('uuid')} is attributed to {author!r}, not {identity_id!r}"
        )


# ── keys ────────────────────────────────────────────────────────────────────


class _Keyring:
    """The identity's keyring in the shadow, with the original graph's era resolvers."""

    def __init__(
        self, root: Path, identity_id: str, boundary: str, key_files: Mapping[str, str]
    ) -> None:
        from rebar._commands.reclaim_collapse import _position_commit_map_for_tracker
        from rebar.reducer import reduce_ticket

        self.root = root
        state = reduce_ticket(str(root / identity_id))
        records = state.get("keyring") if isinstance(state, dict) else None
        self.records = [r for r in records or [] if isinstance(r, dict)]
        self.files = {_key_id(pub): path for pub, path in key_files.items()}
        self.public = {_key_id(r.get("public_key", "")): r["public_key"] for r in self.records}
        self.positions = _position_commit_map_for_tracker(root)
        oracle = authorship.build_ancestry_oracle(str(root))
        self.is_ancestor = oracle or self._subprocess_is_ancestor
        self.boundary = _git(root, "rev-parse", "--verify", f"{boundary}^{{commit}}").strip()

    def _subprocess_is_ancestor(self, ancestor: str, descendant: str) -> bool:
        done = run_git(self.root, "merge-base", "--is-ancestor", ancestor, descendant, check=False)
        return done.returncode == 0

    def require_in_agent(self) -> None:
        if not self.files:
            return
        done = subprocess.run(
            ["ssh-add", "-L"], capture_output=True, text=True, encoding="utf-8", check=False
        )
        loaded = {_key_id(line) for line in done.stdout.splitlines()}
        absent = sorted(set(self.files) - loaded)
        if absent:
            raise ResignError(f"{len(absent)} supplied key(s) are not loaded in the ssh-agent")

    def choose(self, position: str, *, fallback_commit: str | None = None) -> str | None:
        """The supplied key with the latest ``added_at`` that the verifier will accept.

        The verifier runs on the REWRITTEN graph, so validity is evaluated there with the
        shared era rule. Every commit at or below the boundary collapses into the
        checkpoint, so an event below it is checked against that single commit (keys and
        revocations below it are then ordered by position, keys added above it are not
        ancestors). An event above the boundary keeps its original ancestry; the replay
        only ever adds ancestors to it, so the original graph is a safe model.
        """
        commit = self.positions.get(position) or fallback_commit
        if not position or commit is None:
            return None
        if self.is_ancestor(commit, self.boundary):
            valid = authorship.keys_valid_at_anchor(
                self.records,
                _CHECKPOINT,
                position,
                resolve=self._resolve_rewritten,
                is_ancestor=lambda ancestor, descendant: ancestor == descendant,
            )
        else:
            valid = authorship.keys_valid_at_anchor(
                self.records,
                commit,
                position,
                resolve=self.positions.get,
                is_ancestor=self.is_ancestor,
            )
        best: tuple[str, str] | None = None
        for rec in self.records:
            pub = rec.get("public_key", "")
            if pub not in valid or _key_id(pub) not in self.files:
                continue
            added = str(rec.get("added_at") or "")
            if best is None or added > best[0]:
                best = (added, pub)
        return best[1] if best else None

    def _resolve_rewritten(self, position: str) -> str | None:
        commit = self.positions.get(position)
        if commit is None:
            return None
        return _CHECKPOINT if self.is_ancestor(commit, self.boundary) else commit

    def file_for(self, pub: str) -> str:
        return self.files[_key_id(pub)]


# Stand-in for the collapse checkpoint commit, which does not exist until the rewrite runs.
_CHECKPOINT = "checkpoint"


def _key_id(public_key: str) -> str:
    """Compare keys by type and key material, ignoring the comment."""
    return " ".join(public_key.split()[:2])


# ── signing ─────────────────────────────────────────────────────────────────


def _resign_raw(event: dict, pub: str, ring: _Keyring, identity_id: str, signer: Signer) -> bytes:
    event = {k: v for k, v in event.items() if k != "author_sig"}
    if not event.get("author_id"):
        event["author_id"] = identity_id
    content_hash = authorship.authorship_content_hash(event)
    event["author_sig"] = _sign(str(event["uuid"]), content_hash, pub, ring, identity_id, signer)
    return canonical_bytes(event)


def _sign(
    uuid: str, content_hash: str, pub: str, ring: _Keyring, identity_id: str, signer: Signer
) -> str:
    """Sign ``(uuid, content_hash)`` with ``pub`` through the agent and check the result."""
    statement = authorship.build_authorship_statement(uuid, content_hash)
    payload = canonical_str(statement).encode("utf-8")
    try:
        envelope = signer(payload, ring.file_for(pub), identity_id)
    except Exception as exc:  # any signing failure aborts the whole plan
        raise ResignError(f"signing event {uuid} failed: {exc}") from exc
    root = authorship.allowed_signers_from_keys([pub], principal=identity_id)
    verdict = registry.verify(authorship.AUTHORSHIP_KIND, envelope, root)
    subject = json.loads(envelope.payload.decode("utf-8"))["subject"][0]
    if not verdict.verified or subject != {"name": uuid, "digest": {"sha256": content_hash}}:
        raise ResignError(f"new signature for event {uuid} does not verify against its key")
    return dsse.encode(
        envelope.payload_type,
        envelope.payload,
        [{"keyid": s.keyid, "sig": s.sig} for s in envelope.signatures],
    )


# ── git ─────────────────────────────────────────────────────────────────────


# raw-git-ok: read-only rev-parse calls against the disposable shadow clone
def _git(root: Path, *args: str) -> str:
    return run_git(root, *args, check=True).stdout


# raw-git-ok: read-only ls-tree of the disposable shadow clone
def _ls_tree(root: Path, tip: str, tickets: set[str]) -> list[tuple[str, str]]:
    if not tickets:
        return []
    out = run_git(root, "ls-tree", "-r", tip, "--", *sorted(tickets), check=True).stdout
    rows: list[tuple[str, str]] = []
    for line in out.splitlines():
        meta, _, path = line.partition("\t")
        parts = meta.split()
        if len(parts) == 3 and parts[1] == "blob":
            rows.append((parts[2], path))
    return rows
