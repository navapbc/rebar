"""Environment-bound manifest signing for tickets.

This is the cryptographic-attestation surface: a ticket can carry a **manifest of
verified steps** plus an **HMAC-SHA256 signature** computed with a key that is
**specific to the environment** rebar runs in (e.g. an MCP server deployed in a
shared environment). The ``verify-signature`` command then *certifies* that the
recorded steps still match the signature — and, because the key never leaves the
environment, that the signature was produced *here* and not transplanted from
another clone.

Design (mirrors the existing ``.closure-key`` verdict-hash gate in
``compute-verdict-hash.sh``):

* **Key resolution** — ``REBAR_SIGNING_KEY`` (injected out-of-band into a shared
  deployment) wins; otherwise the per-environment ``<tracker>/.signing-key`` file
  (a UUID4 generated on first use, gitignored, never committed, never shared).
  The key is the environment's secret: only a process holding it can produce — or
  certify — a signature.
* **Signed payload** — a canonical JSON serialisation of
  ``{v, algorithm, ticket_id, manifest}``. Binding the ``ticket_id`` stops a
  signature being replayed onto another ticket; binding the whole manifest means
  any edit to the verified-step list invalidates the signature. The signed payload
  deliberately does NOT include volatile git state, so a signature stays
  certifiable as the repo evolves (``head_sha`` is recorded as audit metadata
  only).
* **Key fingerprint** (``key_id``) — a domain-separated SHA-256 prefix of the key,
  stored on the record so verification can distinguish "manifest tampered" from
  "signed by a *different* environment's key" without ever exposing the key.

The signature is persisted as a ``SIGNATURE`` event (append-only, replayed into
``state['signature']`` as last-writer-wins), so it flows through the same locked
write path, auto-push, and compaction as every other event.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import subprocess
import time
import uuid as _uuid
from pathlib import Path

from rebar import config
from rebar._store.canonical import canonical_bytes

logger = logging.getLogger(__name__)

# HMAC over SHA-256. Recorded on every signature so a future algorithm migration
# is detectable on old records rather than silently mis-verified.
ALGORITHM = "HMAC-SHA256"

# Payload schema version (independent of the event SCHEMA_VERSION): bump only if
# the canonical signed-payload shape changes, since that would invalidate every
# prior signature.
PAYLOAD_VERSION = 1


class SigningError(Exception):
    """A signing/verification failure carrying a stderr message + exit code.

    Mirrors ``rebar._commands._seam.CommandError`` so the library facade maps it
    onto ``RebarError`` and the CLI arms reproduce the stderr + exit contract.
    """

    def __init__(self, message: str, returncode: int = 1) -> None:
        super().__init__(message)
        self.message = message
        self.returncode = returncode


# ── Key management (environment-specific secret) ──────────────────────────────
# Sentinel returned when a read-only resolution finds no key. It fingerprints
# deterministically and certifies nothing (a real signature can never match it),
# so a verify on a key-less environment yields unsigned/foreign_key — never a
# false certify — without minting a persistent secret as a side effect.
_NO_KEY = b""


def signing_key(tracker: str | os.PathLike[str], *, create_if_missing: bool = True) -> bytes:
    """Resolve the environment's signing key as raw bytes.

    Order: ``REBAR_SIGNING_KEY`` (non-empty after stripping) > the per-environment
    ``<tracker>/.signing-key`` file. With ``create_if_missing=True`` (signing) a
    missing file is generated as a fresh UUID4 (0o600, atomic). With
    ``create_if_missing=False`` (verifying) a missing file is NOT created — the
    function returns the empty ``_NO_KEY`` sentinel so a read-only verify never
    writes a secret to disk. An empty / whitespace-only key file is treated as
    corruption: a signing caller gets a :class:`SigningError` (an empty key is
    attacker-guessable and must never sign), a verify caller gets ``_NO_KEY``
    (so it certifies nothing). Raises :class:`SigningError` on a real I/O error.
    """
    # Strip surrounding whitespace so an injected key copied with a trailing
    # newline fingerprints identically to the file form (which also strips).
    env_key = os.environ.get("REBAR_SIGNING_KEY")
    if env_key and env_key.strip():
        return env_key.strip().encode("utf-8")

    key_file = Path(tracker) / ".signing-key"
    if not key_file.exists():
        if not create_if_missing:
            return _NO_KEY
        _generate_key_file(key_file)
    try:
        raw = key_file.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise SigningError(f"Error: could not read signing key: {exc}") from None
    if not raw:
        # Empty/whitespace-only key file: an empty key is forgeable by anyone, so
        # it must never be used. Read-only verifies degrade to _NO_KEY (certify
        # nothing); a signing caller must fail loudly rather than emit a forgeable
        # signature.
        if not create_if_missing:
            return _NO_KEY
        raise SigningError(
            f"Error: signing key at {key_file} is empty (corrupt). Remove it to "
            "regenerate, or set REBAR_SIGNING_KEY."
        )
    return raw.encode("utf-8")


def _generate_key_file(key_file: Path) -> None:
    """Atomically create ``key_file`` with a fresh UUID4 key (0o600).

    Write the full key to a unique temp (``mkstemp`` → 0o600, O_EXCL, distinct
    per thread AND per process), then ``os.link`` it into place. The link is
    atomic and fails closed if the target already exists, so exactly ONE creator
    ever lands a key and every reader observes the complete file — never the
    empty/torn window an in-place O_EXCL+write would expose, and never two
    divergent keys for one environment (S1). A lost race (target exists) is
    fine: we drop our temp and the caller reads the winner's key.
    """
    import tempfile

    fd, tmp = tempfile.mkstemp(prefix=".signing-key.", dir=str(key_file.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(str(_uuid.uuid4()) + "\n")
        try:
            os.link(tmp, str(key_file))  # atomic exclusive create
        except FileExistsError:
            pass  # someone else won the race; their key stays
    except OSError as exc:
        raise SigningError(f"Error: could not create signing key at {key_file}: {exc}") from None
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def key_fingerprint(key: bytes) -> str:
    """A short, domain-separated SHA-256 fingerprint of the key (never the key).

    Stored on the signature record as ``key_id`` so verification can report
    "signed by a different environment" distinctly from "manifest altered".
    """
    return hashlib.sha256(b"rebar-signing-key-v1\x00" + key).hexdigest()[:16]


# ── Manifest + payload canonicalisation ───────────────────────────────────────
def parse_manifest(payload) -> list[str]:
    """Validate a manifest into a list of non-empty verified-step strings.

    Accepts an already-parsed list or a JSON-array string. Raises
    :class:`SigningError` (exit 1) with a specific message on any shape error,
    mirroring the leaf-write validators' contract.
    """
    if isinstance(payload, list):
        data = payload
    else:
        try:
            data = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            raise SigningError("Error: manifest argument is not valid JSON") from None
    if not isinstance(data, list):
        raise SigningError("Error: manifest must be a JSON array of verified-step strings")
    if not data:
        raise SigningError("Error: manifest must contain at least one verified step")
    steps: list[str] = []
    for idx, item in enumerate(data):
        if not isinstance(item, str) or not item.strip():
            raise SigningError(f"Error: manifest[{idx}] must be a non-empty string")
        steps.append(item)
    return steps


def _canonical_payload(ticket_id: str, manifest: list[str]) -> bytes:
    """Deterministic bytes signed/verified: sorted-key compact JSON.

    Routed through the canonical seam (:func:`rebar._store.canonical.canonical_bytes`,
    ``ensure_ascii=False``) — byte-identical to the prior inline ``json.dumps``.
    """
    return canonical_bytes(
        {
            "v": PAYLOAD_VERSION,
            "algorithm": ALGORITHM,
            "ticket_id": ticket_id,
            "manifest": manifest,
        }
    )


def compute_signature(ticket_id: str, manifest: list[str], key: bytes) -> str:
    """HMAC-SHA256 hex over the canonical ``(ticket_id, manifest)`` payload."""
    return hmac.new(key, _canonical_payload(ticket_id, manifest), hashlib.sha256).hexdigest()


# ── attested verified_at_sha pin (epic raze-vet-ditch S4) ─────────────────────
# The SHA a gate verified is bound through the EXISTING manifest channel as a manifest
# STEP, NOT a new signed-payload field: the step enters the signed bytes (compute_signature
# signs the whole manifest list) WITHOUT touching `_canonical_payload` or bumping
# PAYLOAD_VERSION — so no prior certified closure is invalidated. The step is shaped as an
# in-toto-style subject so a future move to a DSSE/asymmetric envelope is an envelope swap,
# not a data-shape rewrite (see :func:`verified_at_sha_subject`).
VERIFIED_AT_SHA_PREFIX = "verified-at-sha:"


def verified_at_sha_step(sha: str) -> str:
    """The signed manifest step that pins the verified SHA (``verified-at-sha:<sha>``)."""
    return f"{VERIFIED_AT_SHA_PREFIX}{sha}"


def verified_at_sha_from_manifest(manifest: list[str] | None) -> str | None:
    """Extract the pinned ``verified_at_sha`` from a signed manifest, or ``None``."""
    for step in manifest or []:
        if isinstance(step, str) and step.startswith(VERIFIED_AT_SHA_PREFIX):
            return step[len(VERIFIED_AT_SHA_PREFIX) :] or None
    return None


def verified_at_sha_subject(sha: str, ticket_id: str, predicate_type: str) -> dict:
    """Map the pin to an in-toto v1 Statement subject/predicate shape — the contract that
    makes a future DSSE/asymmetric/transparency-log migration an envelope swap (the same
    ``{name, digest, predicateType}`` data), not a rewrite. The HMAC manifest step
    (:func:`verified_at_sha_step`) is the current trust anchor; this is its in-toto image."""
    return {
        "subject": [{"name": ticket_id, "digest": {"sha1": sha}}],
        "predicateType": predicate_type,
    }


# ── gate-code provenance (which rebar produced an attestation) ────────────────
# Audit/provenance ONLY: recorded in the signed manifest and displayed, NEVER read
# by validity computation. Distinct from ``verified-at-sha`` (the TARGET repo commit
# a plan-review verified) and from ``regver`` (the criteria-registry skew stamp, which
# DOES enforce). See epic jira-reb-596.
REBAR_VERSION_PREFIX = "rebar-version:"


def rebar_version_step(value: str) -> str:
    """The signed manifest step recording the gate code that produced the attestation
    (``rebar-version:<version> (<short-sha>[-dirty])``)."""
    return f"{REBAR_VERSION_PREFIX} {value}"


def rebar_version_from_manifest(manifest: list[str] | None) -> str | None:
    """Extract the gate-code version+SHA provenance stamp, or ``None`` when the manifest
    predates the stamp (epic jira-reb-596)."""
    for step in manifest or []:
        if isinstance(step, str) and step.startswith(REBAR_VERSION_PREFIX):
            return step[len(REBAR_VERSION_PREFIX) :].strip() or None
    return None


def _gate_source_dir() -> str:
    """Directory of the installed rebar package — the gate code doing the certifying.
    signing.py lives in that package, so its own path locates it without importing the
    ``rebar`` facade (which would pull the whole package into the import-cycle graph)."""
    return os.path.dirname(os.path.abspath(__file__))


def _baked_commit_sha() -> str | None:
    """The commit SHA baked into the wheel at build time (``rebar._build_info.COMMIT``),
    or ``None`` when absent (editable/source install, or built outside a git tree). This
    is the non-git fallback for :func:`_gate_commit_sha` (epic jira-reb-596, story 2)."""
    import importlib

    try:
        # Dynamic import: _build_info.py is generated at build time (git-ignored), so it is
        # absent from the source tree that mypy/CI type-checks against.
        mod = importlib.import_module("rebar._build_info")
    except ImportError:
        return None
    commit = getattr(mod, "COMMIT", None)
    return commit or None


def _gate_commit_sha(*, source_dir: str | None = None) -> str | None:
    """Short commit SHA of the rebar SOURCE checkout (the gate code), with a ``-dirty``
    suffix when its working tree has uncommitted changes. Resolution order (epic
    jira-reb-596): live git checkout first (the source of truth in dev/editable installs),
    then the build-baked SHA for non-git (wheel/PyPI) installs, then ``None``. Best-effort:
    any git failure falls through to the baked SHA (+ a debug log)."""
    src = source_dir or _gate_source_dir()
    try:
        out = subprocess.run(
            ["git", "-C", src, "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        logger.debug("gate commit sha: git executable unavailable for %s", src)
        return _baked_commit_sha()
    sha = out.stdout.strip()
    if out.returncode != 0 or not sha:
        logger.debug("gate commit sha: %s is not a live git checkout; using baked SHA", src)
        return _baked_commit_sha()
    # `-dirty` marker — an honest audit needs to distinguish "this exact commit certified"
    # from "some uncommitted variant of it did". Scoped to the rebar source tree.
    try:
        status = subprocess.run(
            ["git", "-C", src, "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=False,
        )
        if status.returncode == 0 and status.stdout.strip():
            sha += "-dirty"
    except OSError:
        pass  # a resolvable HEAD but unresolvable dirty-state — keep the clean SHA
    return sha


def gate_code_version(*, source_dir: str | None = None) -> str:
    """Provenance string for the rebar gate code that produced an attestation:
    ``"<version> (<short-sha>[-dirty])"``, or just ``"<version>"`` when no commit SHA is
    resolvable (a non-git install with no baked SHA). Audit-only; never consumed by the
    claim/close validity computation (epic jira-reb-596)."""
    import importlib.metadata

    # importlib.metadata (not `rebar.__version__`) so this leaf module never imports the
    # rebar facade — keeps signing out of the package import-cycle graph.
    try:
        version = importlib.metadata.version("nava-rebar")
    except importlib.metadata.PackageNotFoundError:
        version = "0+unknown"
    sha = _gate_commit_sha(source_dir=source_dir)
    return f"{version} ({sha})" if sha else version


# ── Verification (pure; no I/O) ───────────────────────────────────────────────
def verify_record(record: dict | None, ticket_id: str, key: bytes) -> dict:
    """Certify a stored signature ``record`` against a freshly recomputed HMAC.

    Returns a verdict dict ``{verified, verdict, reason, ...}`` where ``verdict``
    is one of:

    * ``certified``   — the manifest matches the signature under this key.
    * ``mismatch``    — the steps no longer match (manifest altered / bad sig).
    * ``foreign_key`` — signed by a *different* environment's key, OR this
      environment has no usable key — either way it cannot be certified here.
    * ``unsigned``    — the ticket carries no signature.
    """
    # Fail closed on any malformed record: a non-dict signature value (e.g. a
    # corrupt or forward-compat SNAPSHOT compiled_state) must yield a clean
    # verdict, never an AttributeError that crashes the CLI/MCP caller.
    record = record if isinstance(record, dict) else {}
    raw_manifest = record.get("manifest")
    manifest = raw_manifest if isinstance(raw_manifest, list) else []
    stored_sig = record.get("signature") or ""
    if not isinstance(stored_sig, str):
        stored_sig = ""
    stored_fp = record.get("key_id") or ""
    if not isinstance(stored_fp, str):
        stored_fp = ""

    # Every verdict carries the same keys (uniform contract): consumers can read
    # result["manifest"]/["step_count"] regardless of outcome, including unsigned.
    base = {
        "manifest": manifest,
        "step_count": len(manifest),
        "algorithm": record.get("algorithm"),
        "key_id": stored_fp or None,
        "signed_at": record.get("signed_at"),
        "head_sha": record.get("head_sha"),
        # The attested SHA the verdict was computed against (from the signed manifest step;
        # falls back to the record field). None for legacy/non-attested signatures.
        "verified_at_sha": verified_at_sha_from_manifest(manifest) or record.get("verified_at_sha"),
        # The rebar gate code that produced the attestation (audit/provenance, epic
        # jira-reb-596). None for pre-stamp / unsigned records.
        "rebar_version": rebar_version_from_manifest(manifest),
    }

    if not stored_sig:
        return {
            **base,
            "verified": False,
            "verdict": "unsigned",
            "reason": "ticket has no signature",
        }

    # An empty key (the _NO_KEY sentinel: no .signing-key, no REBAR_SIGNING_KEY, or
    # a corrupt empty key file) can NEVER certify — HMAC under an empty key is
    # forgeable by anyone, so a crafted signature must not be accepted. A key-less
    # environment treats every signature as un-certifiable (foreign).
    if not key:
        return {
            **base,
            "verified": False,
            "verdict": "foreign_key",
            "reason": "this environment has no signing key; it cannot certify any signature",
        }

    local_fp = key_fingerprint(key)
    if stored_fp and stored_fp != local_fp:
        return {
            **base,
            "verified": False,
            "verdict": "foreign_key",
            "reason": (
                f"signature was produced by a different environment key "
                f"(signed with {stored_fp}; this environment is {local_fp})"
            ),
        }

    # No stored fingerprint (a hand-written / forward-compat record) cannot be
    # attributed to an environment — fall through to the HMAC check, which fails
    # CLOSED (mismatch, never certified) when it was actually signed elsewhere.
    expected = compute_signature(ticket_id, manifest, key)
    if hmac.compare_digest(expected, stored_sig):
        return {
            **base,
            "verified": True,
            "verdict": "certified",
            "reason": "verified steps match the signature",
        }
    return {
        **base,
        "verified": False,
        "verdict": "mismatch",
        "reason": (
            "verified steps do NOT match the signature (manifest altered or signature invalid)"
        ),
    }


# ── git audit metadata ────────────────────────────────────────────────────────
def head_sha(repo_root) -> str:
    """Current HEAD sha of ``repo_root``, or ``'unknown'`` when unresolvable.

    Recorded on every signature (audit metadata) and recomputed by the close gate
    for its freshness binding — a public helper so that load-bearing integration
    point isn't reaching into a private name. ``'unknown'`` is a sentinel callers
    must treat as "no resolvable HEAD", never as a matchable value."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return "unknown"
    sha = out.stdout.strip()
    return sha if out.returncode == 0 and sha else "unknown"


# ── Library-facing operations ─────────────────────────────────────────────────
def sign_manifest(ticket_id: str, manifest, *, kind: str | None = None, repo_root=None) -> dict:
    """Sign a manifest of verified steps for a ticket; append a SIGNATURE event.

    Validates the manifest, resolves the ticket id, computes the HMAC with the
    environment key, and persists the signature record through the single locked
    write path. Returns the record (with the resolved ``ticket_id``). Raises
    :class:`SigningError` on a validation/resolve failure.

    ``kind`` (e.g. ``"plan-review"`` / ``"completion-verifier"``) is recorded UNSIGNED on
    the event as a routing hint for the reducer's kind-keyed attestations map (epic
    dark-acme-lumen). It is never authoritative — the reducer derives the kind from the
    signed ``manifest[0]`` and ignores a mismatched hint — so it does not enter the canonical
    signed payload and never invalidates a prior signature. Omitted callers (e.g. `rebar sign`)
    sign exactly as before.
    """
    from rebar._commands._seam import (
        CommandError,
        append_event,
        require_id,
        require_not_ghost,
    )

    if not ticket_id:
        raise SigningError("Error: ticket_id must be non-empty")
    steps = parse_manifest(manifest)

    tracker = config.tracker_dir(repo_root)
    try:
        resolved = require_id(ticket_id, tracker)
        require_not_ghost(resolved, tracker)
    except CommandError as exc:
        raise SigningError(exc.message, exc.returncode) from None

    key = signing_key(str(tracker))
    signature = compute_signature(resolved, steps, key)
    record = {
        "manifest": steps,
        "algorithm": ALGORITHM,
        "signature": signature,
        "key_id": key_fingerprint(key),
        "head_sha": head_sha(config.repo_root(repo_root)),
        # Returned for convenience on this in-memory record. The PERSISTED + queryable value
        # is the signed `verified-at-sha:` manifest step itself: the reducer keeps only the
        # signed fields, and `verify_signature` derives `verified_at_sha` from the manifest —
        # so the trust anchor is always the signed step, never this unsigned echo.
        "verified_at_sha": verified_at_sha_from_manifest(steps),
        "signed_at": time.time_ns(),
    }
    # Unsigned routing hint for the reducer's kind-keyed map; omitted when not provided so
    # existing callers' events are byte-identical to before.
    if kind is not None:
        record["kind"] = kind
    try:
        append_event(resolved, "SIGNATURE", record, tracker, repo_root=repo_root)
    except CommandError as exc:
        raise SigningError(exc.message, exc.returncode) from None
    return {**record, "ticket_id": resolved}


# NOTE: ``retire_attested_pin`` (a write-time clear of the signature on reopen) was REMOVED
# in epic dark-acme-lumen. Attestation records are now immutable and reopen invalidation is
# computed on READ via ``state['last_reopened_at']`` + ``plan_review.attest.compute_validity``
# — which, unlike the old clear, does not destroy the kind-keyed attestations a reopened ticket
# still legitimately carries. See docs/adr/0009-reopen-invalidation-validity-on-read.md.


def _resolve_and_reduce(ticket_id: str, repo_root):
    """Shared verify boilerplate: resolve the id, reduce the ticket, load the key.

    Verify is a READ: never mint a key on disk (a read-only deployment must not write a
    secret). A key-less environment can only ever report unsigned/foreign_key — the honest
    answer. Returns ``(resolved_id, state, key)``; raises :class:`SigningError` (exit 1) when
    the ticket cannot be resolved."""
    from rebar._engine_support import reads
    from rebar._engine_support.resolver import resolve_ticket_id
    from rebar.reducer import reduce_ticket

    if not ticket_id:
        raise SigningError("Error: ticket_id must be non-empty")
    tracker = str(config.tracker_dir(repo_root))
    reads.ensure_fresh(tracker)
    resolved = resolve_ticket_id(ticket_id, tracker)
    if resolved is None:
        raise SigningError(f"Error: ticket '{ticket_id}' not found")
    state = reduce_ticket(os.path.join(tracker, resolved)) or {}
    return resolved, state, signing_key(tracker, create_if_missing=False)


def most_recent_attestation(state: dict):
    """The most-recent signed attestation of ANY kind — the semantics the legacy
    single-slot ``state['signature']`` mirror provided, now sourced from the kind-keyed
    ``state['attestations']`` map (352b contract phase). "Most recent" = the record with
    the greatest ``signed_at`` (ties broken by iteration/replay order, so the last-processed
    wins — matching the mirror's last-writer-wins).

    Falls back to the legacy ``state['signature']`` mirror only when the map is absent/empty
    — e.g. a pre-attestations snapshot the read-side fold-in did not populate. Post-feature
    snapshots always carry ``attestations`` (and old snapshots are folded into it on read),
    so the fallback is a defensive belt-and-suspenders, not the common path."""
    att = state.get("attestations")
    if isinstance(att, dict) and att:
        # max() keeps the LAST max on ties (stable), i.e. the last-processed of equal
        # signed_at — preserving the mirror's replay-order last-writer-wins.
        return max(att.values(), key=lambda r: (r or {}).get("signed_at") or "")
    return state.get("signature")


def _record_for_kind(state: dict, kind: str | None):
    """The signature record to verify for ``kind``. ``kind=None`` returns the most-recent
    attestation of any kind (via :func:`most_recent_attestation` — the pre-attestations
    "verify the latest signature" behavior, now map-sourced). An explicit kind returns
    ``state['attestations'][kind]`` STRICTLY (None when that kind is absent → an honest
    ``unsigned``); a different-kind record is never substituted for a requested kind."""
    if kind is None:
        return most_recent_attestation(state)
    att = state.get("attestations")
    return att.get(kind) if isinstance(att, dict) else None


def verify_signature(ticket_id: str, *, kind: str | None = None, repo_root=None) -> dict:
    """Certify a ticket's recorded verified-steps against its signature.

    Resolves the id, reduces the ticket, and verifies one signature record with the
    environment key. Returns the verdict dict (see :func:`verify_record`) with the resolved
    ``ticket_id`` attached. Raises :class:`SigningError` (exit 1) only when the ticket itself
    cannot be resolved.

    ``kind`` selects WHICH attestation to verify (epic dark-acme-lumen): ``None`` (default)
    verifies the legacy most-recent ``signature`` mirror — exact pre-attestations behavior, so
    every existing no-kind caller is unchanged — while an explicit kind (e.g. ``"plan-review"``
    / ``"completion-verifier"``) verifies THAT kind strictly from the kind-keyed map. Use
    :func:`verify_attestations` for all kinds at once."""
    resolved, state, key = _resolve_and_reduce(ticket_id, repo_root)
    result = verify_record(_record_for_kind(state, kind), resolved, key)
    result["ticket_id"] = resolved
    if kind is not None:
        result["kind"] = kind
    return result


def verify_attestations(ticket_id: str, *, repo_root=None) -> dict:
    """Verify EVERY attestation kind on a ticket: returns ``{kind: verdict_dict}`` (each a
    :func:`verify_record` result with ``ticket_id`` + ``kind`` attached), kinds sorted. ``{}``
    when the ticket carries no attestations. No LLM, no network — a pure local HMAC verify per
    kind. Raises :class:`SigningError` only when the ticket cannot be resolved."""
    resolved, state, key = _resolve_and_reduce(ticket_id, repo_root)
    att = state.get("attestations")
    out: dict = {}
    if isinstance(att, dict):
        for k in sorted(att):
            r = verify_record(att[k], resolved, key)
            r["ticket_id"] = resolved
            r["kind"] = k
            out[k] = r
    return out


# ── CLI arms (in-process dispatch from rebar._cli) ────────────────────────────
def sign_cli(argv: list[str]) -> int:
    """``rebar sign <ticket_id> <manifest_json> [--output json]``."""
    import sys

    from rebar._engine_support.output import OutputFormatError, parse_output

    try:
        fmt, rest = parse_output(argv, "report")
    except OutputFormatError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 2
    if len(rest) < 2:
        sys.stderr.write("Usage: rebar sign <ticket_id> <manifest_json>\n")
        return 1
    try:
        record = sign_manifest(rest[0], rest[1])
    except SigningError as exc:
        sys.stderr.write(exc.message + "\n")
        return exc.returncode
    if fmt == "json":
        sys.stdout.write(json.dumps(record, ensure_ascii=False) + "\n")
    else:
        sys.stdout.write(
            f"SIGNED {record['ticket_id']} "
            f"steps={len(record['manifest'])} "
            f"key={record['key_id']} "
            f"sig={record['signature'][:16]}…\n"
        )
    return 0


def verify_signature_cli(argv: list[str]) -> int:
    """``rebar verify-signature <ticket_id> [--kind <kind>] [--output json]``.

    Verifies a SINGLE attestation and returns its verdict (json = the verdict dict, report =
    one ``SIGNATURE: …`` line) — exit 0 iff ``certified``; exit 1 for
    mismatch/foreign_key/unsigned; the SigningError exit code on an unresolved ticket.
    Without ``--kind`` this verifies the most-recent signature (exact pre-attestations
    behavior). With ``--kind K`` (``--kind K`` or ``--kind=K``) it verifies that kind strictly
    from the kind-keyed map — the per-ticket CI-gate form (epic dark-acme-lumen). The full
    per-kind set is available via the library ``verify_attestations`` and the ``attestations``
    field of ``rebar show``.
    """
    import sys

    from rebar._engine_support.output import (
        OutputFormatError,
        error_envelope,
        parse_output,
    )

    try:
        fmt, rest = parse_output(argv, "report")
    except OutputFormatError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 2
    # Parse --kind (accept both the space and equals form, matching the read-CLI convention).
    kind: str | None = None
    pos: list[str] = []
    i = 0
    while i < len(rest):
        a = rest[i]
        if a == "--kind" and i + 1 < len(rest):
            kind = rest[i + 1]
            i += 2
            continue
        if a.startswith("--kind="):
            kind = a[len("--kind=") :]
            i += 1
            continue
        pos.append(a)
        i += 1
    if len(pos) < 1:
        sys.stderr.write("Usage: rebar verify-signature <ticket_id> [--kind <kind>]\n")
        return 1
    try:
        result = verify_signature(pos[0], kind=kind)
    except SigningError as exc:
        if fmt == "json":
            sys.stdout.write(
                json.dumps(
                    error_envelope("ticket_not_found", pos[0], exc.message, exc.returncode),
                    ensure_ascii=False,
                )
                + "\n"
            )
        sys.stderr.write(exc.message + "\n")
        return exc.returncode
    if fmt == "json":
        sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
    else:
        label = f"SIGNATURE[{kind}]" if kind else "SIGNATURE"
        sys.stdout.write(f"{label}: {result['verdict']} — {result['reason']}\n")
    return 0 if result["verified"] else 1
