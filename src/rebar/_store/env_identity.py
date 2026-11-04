"""Mint and diagnose the ignored ``.env-id`` environment identity.

Every event carries this environment's identifier, which is also the principal of
its op-cert attestations under ADR 0044. Re-cloning a tracker copies events but omits
this ignored state, so the clone mints a new identity and its audit trail splits from
the old one (bug gold-distinct-lacewing). The old identity's attestations still verify
on the public key embedded in each signature; they turn ``foreign_key`` only under the
opt-in ``verify.require_environment`` (bug c21f-6f29-5d2d-4a5a).

This module applies three minting policies.

- **Genesis:** Mint without a warning when the store has no events.
- **Populated store:** Mint and warn on stderr with prior identifiers,
  attestation consequences, and state that can be carried over.
- **Acknowledged mint:** ``REBAR_ALLOW_ENV_REIDENTIFY=1`` reduces that warning to
  one note without gating the operation.

Minting proceeds because the store cannot distinguish a re-clone from an
additional collaborator, which requires its own identity. The environment
variable reaches CLI, MCP boot, and library ensure paths. A CLI flag would not.

:func:`divergence_report` detects one author writing under two identities at once. It
scopes its signal by author so normal multi-clone stores do not warn, and by time so
successive re-clones of one store do not warn either. Existing events keep
their original identity and attestations are not re-homed. Cross-environment key
publication would change the trust model and remains outside this module.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from collections.abc import Mapping

from rebar._store.ensures import EnsureOutcome

#: The git-ignored per-environment identity file, relative to the tracker root.
ENV_ID_FILE = ".env-id"

#: Explicit operator acknowledgement that minting into a populated store is intended.
#: Spelled as a literal at the read site in :func:`override_enabled` too — the
#: ``docs/env-vars.md`` generator resolves only string-literal keys, and a var it cannot
#: name ships undocumented (bug b00f's lesson). ``test_override_env_name_matches_the_read_site``
#: pins the two spellings together so they cannot drift.
OVERRIDE_ENV = "REBAR_ALLOW_ENV_REIDENTIFY"

#: The git-ignored local state a tracker re-clone must carry over, with what breaks per
#: file when it does not. Rendered into the warning so the remedy travels with the alarm.
CARRY_OVER = (
    (
        ENV_ID_FILE,
        "this environment's identity — stamped into every event and the "
        "principal of every op-cert attestation",
    ),
    (
        ".opcert-key",
        "the op-cert signing key — carry it with .env-id: under the old id, the "
        "first op-cert this clone signs mints a new key, and attestations the old "
        "key signed then fail as `mismatch` unless that key is pinned",
    ),
    (
        ".opcert-key.pub",
        "the op-cert public key — the verifier reads it (or re-derives it from the private key)",
    ),
    (
        ".ensure-applied",
        "the ensure-registry marker — absent, every unit simply re-runs "
        "and re-converges (harmless, but noisy)",
    ),
)

# Bound the discovery scan. It only runs when `.env-id` is ABSENT (a re-clone or a
# genesis init), never on a converged store, so the caps exist to keep the pathological
# case cheap rather than to trade away accuracy: a handful of distinct ids is already
# conclusive evidence that this store was written elsewhere.
_MAX_DISTINCT_IDS = 4
_MAX_FILES_SCANNED = 2000


def read_env_id(tracker: str | os.PathLike) -> str:
    """This store's current environment id, or ``""`` when absent/unreadable."""
    try:
        with open(os.path.join(os.fspath(tracker), ENV_ID_FILE), encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def override_enabled() -> bool:
    """True when the operator has explicitly acknowledged the re-identification.

    Resolved through the owned config seam
    (:func:`rebar.config.resolve_allow_env_reidentify`) rather than ``os.environ`` here."""
    from rebar import config

    return config.resolve_allow_env_reidentify()


def store_event_env_ids(
    tracker: str | os.PathLike,
    *,
    max_ids: int = _MAX_DISTINCT_IDS,
    max_files: int = _MAX_FILES_SCANNED,
) -> set[str]:
    """The distinct ``env_id`` values recorded in this store's event files.

    Bounded and best-effort: stops at ``max_ids`` distinct ids or ``max_files`` files
    read, and skips anything unreadable or malformed. Used to answer one question —
    "was this store written by some environment other than the one about to be minted?"
    — for which a bounded sample is sufficient evidence.
    """
    root = os.fspath(tracker)
    found: set[str] = set()
    read = 0
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return found
    for name in entries:
        if name.startswith("."):
            continue  # store artifacts (.git, .env-id, .opcert-key…) are never tickets
        ticket_dir = os.path.join(root, name)
        try:
            files = sorted(os.listdir(ticket_dir))
        except OSError:
            continue  # a plain file at the top level, or an unreadable directory
        for filename in files:
            if filename.startswith(".") or not filename.endswith(".json"):
                continue
            if read >= max_files or len(found) >= max_ids:
                return found
            read += 1
            try:
                with open(os.path.join(ticket_dir, filename), encoding="utf-8") as fh:
                    event = json.load(fh)
            except (OSError, ValueError):
                continue  # corrupt/partial events are fsck's business, not identity's
            if isinstance(event, dict):
                env_id = event.get("env_id")
                if isinstance(env_id, str) and env_id.strip():
                    found.add(env_id.strip())
    return found


def _carry_over_lines() -> str:
    return "\n".join(f"    {name} — {why}" for name, why in CARRY_OVER)


def reidentification_warning(prior_ids: set[str], *, acknowledged: bool) -> str:
    """The warning printed when a NEW identity is minted into a populated store.

    The store cannot tell the two cases apart, so the text does not pretend to: a second
    collaborating clone legitimately needs its own identity, while a RE-clone of an
    environment you already worked in has just split its audit trail. Both need
    the same fact — the prior env id(s) — and only the operator knows which case it is.
    """
    ids = ", ".join(sorted(prior_ids))
    if acknowledged:
        return (
            f"NOTE: minted a new environment identity into a store already holding events "
            f"from {ids} (acknowledged via {OVERRIDE_ENV})."
        )
    return (
        "WARNING: minted a NEW environment identity for a store that already holds events "
        f"written by another environment ({ids}).\n"
        "  If this is an additional clone collaborating on a shared store, that is normal "
        "— it simply starts with no attestations of its own.\n"
        "  If this is a RE-CLONE of a tracker you were already working in, you have just "
        "lost that environment's identity. Its op-cert attestations still certify here on "
        "the public key embedded in each signature; they turn `foreign_key` only under the "
        "opt-in `verify.require_environment`, which trusts only the own or pinned keys of "
        "the environment it names. What is lost is one continuous audit identity (new "
        "events carry the new id) and the ability to sign as the old identity. Recover both "
        "by copying this "
        "git-ignored state out of the old tracker and re-running the command:\n"
        f"{_carry_over_lines()}\n"
        "  Prior events are NOT wrong — they are correctly stamped with whoever wrote "
        "them — so rebar will not rewrite them.\n"
        f"  Set {OVERRIDE_ENV}=1 to acknowledge this and quieten the warning."
    )


def mint_env_id(tracker: str | os.PathLike) -> str:
    """Write a fresh ``.env-id`` unconditionally and return it. No guard — callers that
    can re-identify the environment must go through :func:`ensure_env_id_unit`."""
    env_id = str(uuid.uuid4())
    with open(os.path.join(os.fspath(tracker), ENV_ID_FILE), "w", encoding="utf-8") as fh:
        fh.write(env_id + "\n")
    return env_id


def mint_env_id_guarded(tracker: str | os.PathLike) -> EnsureOutcome:
    """Mint a missing ``.env-id`` and warn over another environment's events.

    This shared decision keeps the ensure unit and fresh-init bootstrap aligned.
    Minting never refuses because an additional collaborating clone also requires
    a distinct identity. The operator receives one warning when prior events make
    the two cases indistinguishable.
    """
    root = os.fspath(tracker)
    if not os.path.isdir(root):
        return EnsureOutcome("env-id", "ok", "tracker dir absent")
    if os.path.isfile(os.path.join(root, ENV_ID_FILE)):
        return EnsureOutcome("env-id", "ok", f"{ENV_ID_FILE} present")
    prior = store_event_env_ids(root)
    minted = mint_env_id(root)
    if not prior:
        return EnsureOutcome("env-id", "changed", f"generated {ENV_ID_FILE}")
    acknowledged = override_enabled()
    sys.stderr.write(reidentification_warning(prior, acknowledged=acknowledged) + "\n")
    detail = f"generated {ENV_ID_FILE} ({minted}) into a store holding events from " + ", ".join(
        sorted(prior)
    )
    return EnsureOutcome("env-id", "changed", detail)


def ensure_env_id_unit(tracker: str) -> EnsureOutcome:
    """Ensure a stable ``.env-id``, warning when prior events use another identity."""
    return mint_env_id_guarded(_realpath(tracker))


def _realpath(p: str) -> str:
    return os.path.realpath(p)


#: One author's ``(first, last)`` event timestamps under one env id, or ``None`` when unknown.
Span = tuple[int, int] | None


def _overlaps(a: Span, b: Span) -> bool:
    """Whether two event time spans share any instant, as CLOSED intervals.

    A successor clone's first write comes after its predecessor's last write, so a shared
    endpoint is not evidence of succession and counts as overlap. Measured against the
    live store's 2026-08-09 handover (the retiring clone last wrote at 19:51:58 PDT and its
    successor first wrote at 20:44:59 PDT), succession leaves a gap of minutes, not a
    shared nanosecond, so the closed rule loses nothing there. Event timestamps are HLC values
    (:func:`rebar._store.hlc.next_tick`): never earlier than the writer's wall clock, but
    possibly later, so clock skew between hosts larger than the gap between two clones can
    misclassify them in either direction. An unknown span (``None``) cannot be ordered, so
    it counts as overlapping rather than being assumed successive.
    """
    if a is None or b is None:
        return True
    return a[0] <= b[1] and b[0] <= a[1]


def divergence_report(current: str, spans: Mapping[tuple[str, str], Span]) -> str | None:
    """Return the ``fsck`` line for one author writing under two identities at once.

    ``spans`` maps ``(env_id, author)`` to that author's event time span under that env.
    Author scoping distinguishes re-identification from a healthy store shared by
    multiple clones. Time scoping (bug disruptive-breezy-esok, operator decision
    2026-11-03) treats an author's env ids whose spans do NOT overlap as successive
    re-clones of one store and does not report them. The accepted trade-off is that a
    clone retired and then replaced is not flagged. The authors associated with
    ``current`` come from store events, so this check needs no configured identity.
    """
    if not current:
        return None  # not yet identified — the mint warning's business, not the detector's
    local = {author: span for (env_id, author), span in spans.items() if env_id == current}
    foreign = sorted(
        {
            env_id
            for (env_id, author), span in spans.items()
            if env_id != current and author in local and _overlaps(local[author], span)
        }
    )
    if not foreign:
        return None
    return (
        f"ENV_ID_MISMATCH: this environment ('{current}') has also written events under "
        f"{len(foreign)} other environment id(s) ({', '.join(foreign)}) — the same "
        "author(s) wrote under both during overlapping periods, so these are not "
        "successive re-clones. Op-cert attestations signed by those identities still "
        "certify here on the public key embedded in each signature; they turn "
        "`foreign_key` only under the opt-in `verify.require_environment`. This is the "
        "signature of a second clone writing while the first was "
        "still active, or of a re-clone that did not carry over its git-ignored local "
        f"state ({', '.join(name for name, _ in CARRY_OVER)}) while the old clone kept writing."
    )
