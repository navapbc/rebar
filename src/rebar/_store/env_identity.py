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

:func:`divergence_report` detects one env id signed by two or more authorship keys at
once, which means a ``.env-id`` was copied into another environment: each environment signs
with its own key, and checkouts in one environment may share it (operator decision on bug
placid-dreary-zethuswasp, 2026-11-18). Several env ids under one key, and an env id whose
keys follow one another (a rotation), are not reported. Existing events keep their
original identity and attestations are not re-homed. Cross-environment key publication
would change the trust model and remains outside this module.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone

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
        "first op-cert this clone signs mints a new key. Attestations the old key "
        "signed still verify on the key embedded in each signature, but an environment "
        "whose `verify.trusted_opcert_keys` list omits the old environment-and-key pair "
        "reads them as `untrusted`",
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
        "the public key embedded in each signature, because verification checks validity "
        "only by default. A verifying environment that sets `verify.trusted_opcert_keys` "
        "trusts them only while its list names the old environment-and-key pair (otherwise "
        "they read "
        "`untrusted`), and the opt-in `verify.require_environment` turns a certificate "
        "naming another environment `foreign_key`. What is lost is one continuous audit "
        "identity (new "
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


#: One signing key's ``(first, last)`` event timestamps (HLC nanoseconds) under one env id.
Span = tuple[int, int]


def _overlaps(a: Span, b: Span) -> bool:
    """Whether two event time spans share any instant, as CLOSED intervals.

    A rotated key's successor first signs after its predecessor's last signature, so a
    shared endpoint is not evidence of succession and counts as overlap. Event timestamps
    are HLC values (:func:`rebar._store.hlc.next_tick`): never earlier than the writer's
    wall clock, but possibly later, so clock skew between hosts larger than the gap at a
    rotation can misclassify it.
    """
    return a[0] <= b[1] and b[0] <= a[1]


def _utc(ts: int) -> str:
    return datetime.fromtimestamp(ts / 1e9, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _overlapping_keys(keys: Mapping[str, Span]) -> list[str]:
    """The keys whose spans overlap at least one other key's span, sorted."""
    return sorted(
        {
            key
            for key, span in keys.items()
            for other, other_span in keys.items()
            if other != key and _overlaps(span, other_span)
        }
    )


def divergence_report(spans: Mapping[tuple[str, str], Span]) -> str | None:
    """Return the ``fsck`` line for env ids signed by two or more keys at once, or ``None``.

    ``spans`` maps ``(env_id, signing-key fingerprint)`` to that key's event time span under
    that env id, store-wide. Only overlapping keys under ONE env id are reported: several
    env ids under one key are checkouts of one environment, and sequential keys are a
    rotation. Events without a parseable signature carry no key and never reach ``spans``.
    """
    by_env: dict[str, dict[str, Span]] = {}
    for (env_id, key), span in spans.items():
        by_env.setdefault(env_id, {})[key] = span
    findings = []
    for env_id in sorted(by_env):
        keys = by_env[env_id]
        clashing = _overlapping_keys(keys)
        if clashing:
            detail = "; ".join(
                f"{key} {_utc(keys[key][0])}..{_utc(keys[key][1])}" for key in clashing
            )
            findings.append(f"{env_id} ({detail})")
    if not findings:
        return None
    return (
        f"ENV_ID_MISMATCH: {len(findings)} environment id(s) signed by two or more keys "
        f"during overlapping periods: {', '.join(findings)}. Each environment signs with its "
        f"own key, so this is the signature of a {ENV_ID_FILE} copied into another "
        f"environment. In the checkout that should not carry the id, delete {ENV_ID_FILE} "
        "(and any copied .opcert-key / .opcert-key.pub) so it mints its own identity. "
        "Events already written keep their ids, so this note is report-only and does not "
        "count toward fsck's exit status."
    )
