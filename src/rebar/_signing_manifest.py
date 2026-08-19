"""Signed-manifest vocabulary and the gate-code provenance behind it.

The bottom layer of the ``rebar.signing`` family and a pure leaf: stdlib only, no ``rebar``
imports, so it can never participate in an import cycle. It owns what a signed manifest IS —
the shared error type, manifest validation, and the two step vocabularies a manifest carries
(``verified-at-sha:`` and ``rebar-version:``) — together with the git/build provenance that
produces the ``rebar-version:`` value. Split out of ``signing.py`` (story f5c1-e41d) along the
seam that already existed there; ``rebar.signing`` re-exports everything below, so importers
are unchanged.

**Patch these symbols HERE, not on ``rebar.signing``.** ``_gate_commit_sha`` reads
``_baked_commit_sha`` as a bare global, i.e. out of THIS module's namespace, so a
``monkeypatch.setattr(signing, "_baked_commit_sha", ...)`` reaches the re-exported alias and
silently does nothing. ``tests/unit/test_signing_module_split.py`` guards that positively.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess

logger = logging.getLogger(__name__)


class SigningError(Exception):
    """A signing/verification failure carrying a stderr message + exit code.

    Mirrors ``rebar._commands._seam.CommandError`` so the library facade maps it
    onto ``RebarError`` and the CLI arms reproduce the stderr + exit contract.
    """

    def __init__(self, message: str, returncode: int = 1) -> None:
        super().__init__(message)
        self.message = message
        self.returncode = returncode


# ── Manifest validation ───────────────────────────────────────────────────────
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
    This module lives in that package, so its own path locates it without importing the
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


# ── git audit metadata ────────────────────────────────────────────────────────
def head_sha(repo_root) -> str:
    """Current HEAD sha of ``repo_root``, or ``'unknown'`` when unresolvable. It is audit
    metadata and a public freshness-binding helper. Callers must treat
    ``'unknown'`` as "no resolvable HEAD", never as a matchable value."""
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
