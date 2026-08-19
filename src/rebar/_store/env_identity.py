"""Environment identity (``.env-id``) — minting, the re-identification guard, and
the store-wide divergence report.

``.env-id`` is a per-environment uuid that is **git-ignored local state** and is
**stamped into every event** this environment writes. It is also the ``principal`` of
every op-cert attestation (ADR 0044): an attestation is verified against the LOCAL
op-cert key, so a certificate whose principal is some *other* environment can never be
verified here — the key that signed it is not in this store.

That makes a re-clone of the tracker a silent identity change. A fresh clone carries the
events but not the git-ignored local state, so ``.env-id`` is absent and the naive
"mint one if missing" rule invents a NEW identity while the store is full of events (and
attestations) belonging to the old one. Nothing fails at that moment; the loss surfaces
much later as a ``foreign_key`` verdict at a claim gate, and it reads as a per-ticket
problem when it is store-wide (bug gold-distinct-lacewing).

This module is the single place that decides how loud a mint is:

* **Genesis** — no events in the store ⇒ mint silently. First-time ``rebar init`` is
  unchanged.
* **Into a populated store** — mint, and warn loudly to stderr, naming the prior
  environment id(s), the attestation consequence, and the state to carry over.
* **Override** — ``REBAR_ALLOW_ENV_REIDENTIFY=1`` acknowledges that and quietens the
  warning to a one-line note. It gates nothing.

It **warns rather than refuses** because the store cannot tell a re-clone from a second
clone collaborating on a shared tickets branch, and the latter is a first-class workflow
that legitimately needs its own identity (refusing broke
``tests/integration/test_concurrency_regression.py::
test_two_clone_union_deterministic_replay_and_fork_tiebreak``). Only the operator knows
which case it is, so the mint proceeds and the operator is told.

The override is an environment variable rather than a CLI flag because the mint happens
inside the ensure registry, which runs from the CLI, the MCP server's boot path and the
library alike; a flag on ``rebar init`` would cover only one of the three.

:func:`divergence_report` is the durable half: it detects a re-identification that already
happened, and is scoped by AUTHOR precisely because several env ids in one store is the
healthy multi-clone shape — see its docstring.

None of this is a migration: prior events stay stamped with the environment that actually
wrote them (they are history, not corruption), and no attestation is re-homed. Making an
old attestation verifiable again requires publishing per-environment op-cert PUBLIC keys
into the store — a trust-model change that needs its own ADR and is out of scope here.
"""

from __future__ import annotations

import json
import os
import sys
import uuid

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
        "the op-cert signing key — without it no existing attestation "
        "verifies, even with the matching .env-id",
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
    environment you already worked in has just orphaned its own attestations. Both need
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
        "lost that environment's identity: every op-cert attestation it signed is now "
        "unverifiable here (a `foreign_key` verdict at the claim/close gates) and must be "
        "re-earned. Recover it by copying this git-ignored state out of the old tracker "
        "and re-running the command:\n"
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
    """Mint ``.env-id`` if absent, never SILENTLY over another environment's events.

    The shared decision point behind both the ensure unit and fresh-init's local-file
    bootstrap, so the two cannot drift into disagreeing about when a mint is loud.

    It mints in every case. Refusing was tried and is wrong: a second clone mounting a
    shared tickets branch is a first-class workflow (``tests/integration/
    test_concurrency_regression.py::test_two_clone_union_deterministic_replay_and_fork_tiebreak``
    is exactly that), and a store cannot distinguish it from a re-clone. So the mint
    proceeds and the operator — who CAN tell them apart — is told, loudly, once.
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
    """Ensure the store carries a stable ``.env-id`` (ensure-registry unit).

    Check-then-act: no-ops on a store that already has one, and mints loudly rather than
    silently when the store already holds another environment's events."""
    return mint_env_id_guarded(_realpath(tracker))


def _realpath(p: str) -> str:
    return os.path.realpath(p)


def divergence_report(current: str, seen: set[tuple[str, str]]) -> str | None:
    """The ``fsck`` line for an environment that has written under more than one identity,
    or None. ``seen`` is the store's ``(env_id, author)`` pairs.

    The DETECTOR for a re-identification that already happened — including one that
    predates the mint warning, or one the operator acknowledged and then forgot.

    Scoped by AUTHOR, which is what makes it a signal rather than noise. A store shared by
    several clones always holds several env ids; that is the healthy topology, and
    reporting it would fire on every well-formed team store until nobody read the line.
    What is NOT healthy is one author writing under two identities: either their tracker
    was re-cloned, or they moved machines. Both mean that author's own attestations are
    dead here, which is precisely the loss this check exists to surface — and the authors
    "here" are read from the store itself (whoever writes under the current env id), so
    no configuration is consulted.
    """
    if not current:
        return None  # not yet identified — the mint warning's business, not the detector's
    local_authors = {author for env_id, author in seen if env_id == current}
    foreign = sorted(
        {env_id for env_id, author in seen if env_id != current and author in local_authors}
    )
    if not foreign:
        return None
    return (
        f"ENV_ID_MISMATCH: this environment ('{current}') has also written events under "
        f"{len(foreign)} other environment id(s) ({', '.join(foreign)}) — the same "
        "author(s) appear under both. Op-cert attestations signed by those identities "
        "cannot be verified here (`foreign_key` at the claim/close gates) and must be "
        "re-earned. This is the signature of a tracker re-clone that did not carry over "
        f"its git-ignored local state ({', '.join(name for name, _ in CARRY_OVER)})."
    )
