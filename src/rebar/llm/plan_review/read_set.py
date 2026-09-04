"""The review READ-SET and glob-aware dependency entries (ticket 81ca).

ADR 0002 scopes attestation currency to a per-path ``{path: sha256}`` dependency set built
from the ticket's declared ``file_impact`` ∪ the files the review CITED. When a ticket
declares NO ``file_impact`` that set is empty, and the claim gate degrades to whole-HEAD
freshness — a deliberate fail-safe, but one measured to over-trigger badly (21% of full
re-reviews ran on byte-identical material; analysis ticket b902).

This module supplies the two primitives that let the NO-``file_impact`` case reuse the SAME
per-path machinery instead of falling back:

* :func:`normalize_read_set` — the deterministic projection from the agentic passes'
  ``distinct_fetches`` telemetry (:func:`rebar.llm.usage_log.fetch_target`) to repo-relative
  POSIX file paths that :func:`~rebar.llm.plan_review.manifest._hash_file` can actually hash.
* :func:`hash_dep_entry` — the ONE helper BOTH the signing side
  (:func:`~rebar.llm.plan_review.manifest.dependency_hashes`) and the claim-gate re-check
  (:func:`~rebar.llm.plan_review.attest.compute_validity`) route through, so a glob entry's
  digest cannot be computed two different ways. A plain path delegates to ``_hash_file``
  unchanged; a GLOB entry yields a MEMBERSHIP digest.

Why a membership digest. ADR 0002's currency check iterates the concrete entries recorded at
signing time. A glob expanded to per-file ``dep`` lines therefore catches CONTENT drift in the
files that existed then, but is blind to a file ADDED under the glob afterwards — precisely the
churn (a new reviewer prompt, a new gate YAML) the blast radius exists to guard. Recording the
glob PATTERN itself as an additional entry whose digest covers its sorted matched-path LIST
closes that hole without changing the shape of the signed map or the comparison loop.
"""

from __future__ import annotations

import glob as _glob
import hashlib
import logging
import os
from collections.abc import Iterable, Sequence
from pathlib import PurePath
from typing import Any

logger = logging.getLogger(__name__)

_GLOB_META = ("*", "?", "[")

#: The fixed blast-radius entries that guard a NO-``file_impact`` attestation even when the
#: review read and cited nothing: the criteria machinery (the threshold resolver and
#: overlay-merge core, which change a review's blocking/advisory outcome with no rubric edit),
#: the routing overlays, the reviewer rubrics, the gate workflows, and the project config.
BLAST_RADIUS_ENTRIES: tuple[str, ...] = (
    ".rebar/criteria_routing.json",
    "rebar.toml",
    "src/rebar/llm/criteria/**",
    "src/rebar/llm/plan_review/criteria_routing.json",
    "src/rebar/llm/reviewers/*.md",
    "src/rebar/llm/workflow/gates/*.yaml",
)

#: The only fetch tool whose ``target`` is a repository FILE path. ``search_files`` targets are
#: query strings and ``list_directory`` targets are directories (see
#: :func:`rebar.llm.usage_log.fetch_target`); neither is hashable content the review grounded on.
_PATH_BEARING_TOOL = "read_file"


def is_glob(entry: str) -> bool:
    """True when a dependency entry is a PATTERN naming a set, not a single file."""
    return any(token in entry for token in _GLOB_META)


def expand_glob(pattern: str, *, base: str) -> list[str]:
    """The sorted repo-relative POSIX paths of the REGULAR FILES ``pattern`` matches under
    ``base``. Directories and unreadable entries are skipped, so every returned path is one
    :func:`~rebar.llm.plan_review.manifest._hash_file` can hash."""
    try:
        matches = _glob.glob(pattern, root_dir=base, recursive=True)
    except (OSError, ValueError):  # pragma: no cover - defensive; glob rarely raises
        logger.warning("blast-radius glob %r could not be expanded", pattern, exc_info=True)
        return []
    out: set[str] = set()
    for match in matches:
        rel = PurePath(match).as_posix()
        if rel and os.path.isfile(os.path.join(base, match)):
            out.add(rel)
    return sorted(out)


def glob_membership_digest(pattern: str, *, base: str) -> str:
    """SHA-256 over the sorted, newline-joined MATCH LIST of ``pattern`` under ``base``.

    Covers set MEMBERSHIP only — the members' contents are covered by their own expanded
    ``dep`` entries. An addition or a deletion under the glob moves this digest even though
    the added file has no baked per-file hash, which is the blind spot a purely per-path
    expansion leaves open."""
    listing = "\n".join(expand_glob(pattern, base=base))
    return hashlib.sha256(listing.encode("utf-8")).hexdigest()


def hash_dep_entry(entry: str, *, base: str) -> str:
    """Digest ONE recorded dependency entry, dispatching on its kind.

    The single shared boundary for both the signing side and the claim-gate re-check: a glob
    entry yields its membership digest, any other entry delegates to the unchanged whole-file
    hash (so every pre-existing manifest re-hashes byte-identically to before)."""
    from .manifest import _hash_file

    if is_glob(entry):
        return glob_membership_digest(entry, base=base)
    return _hash_file(entry, base=base)


def blast_radius_paths(*, base: str) -> list[str]:
    """Every dependency entry the blast radius contributes: each configured entry itself
    (a glob is recorded as a membership entry) plus, for a glob, its expanded members as
    ordinary per-path entries so content drift names the exact file that moved."""
    out: set[str] = set()
    for entry in BLAST_RADIUS_ENTRIES:
        out.add(entry)
        if is_glob(entry):
            out.update(expand_glob(entry, base=base))
    return sorted(out)


def normalize_read_set(fetches: Iterable[Any], *, base: str) -> list[str]:
    """Project agentic-pass ``distinct_fetches`` telemetry onto the signed read-set.

    ``fetches`` is the ``[{"tool": ..., "target": ...}, ...]`` list
    :func:`rebar.llm.usage_log.run_shape` accumulates. The projection is deterministic and
    order-independent, applying in order:

    1. **Tool filter** — keep only ``read_file`` entries. ``search_files`` targets are query
       STRINGS and ``list_directory`` targets are DIRECTORIES; neither can be whole-file
       hashed, and expanding a directory would import files the review never read.
    2. **Root resolution** — resolve a relative target against ``base`` (the review's hash
       root: the pinned-SHA snapshot during an attested review, else the checkout).
    3. **Containment** — drop anything that does not lie under ``base`` after
       :func:`os.path.realpath`, which discards ``../`` escapes and absolute paths outside
       the repository.
    4. **Repo-relative POSIX form** — re-express the survivor relative to ``base`` with ``/``
       separators, which also collapses a leading ``./`` and any interior ``.``/``..``.
    5. **Existence filter** — drop anything that is not an existing regular file. A mistyped
       or speculative read contributed no content to the review, so baking an ``absent``
       hash for it would let an unrelated later file creation invalidate the attestation —
       the exact over-invalidation this scoping removes.
    6. **Dedupe + sort** — two spellings of one file collapse to one entry and the signed
       manifest is reproducible.
    """
    try:
        root = os.path.realpath(base)
    except OSError:  # pragma: no cover - defensive
        return []
    out: set[str] = set()
    for fetch in fetches or ():
        if not isinstance(fetch, dict) or fetch.get("tool") != _PATH_BEARING_TOOL:
            continue
        target = fetch.get("target")
        if not isinstance(target, str) or not target:
            continue
        rel = _relative_under(target, root=root)
        if rel is not None:
            out.add(rel)
    return sorted(out)


def _relative_under(target: str, *, root: str) -> str | None:
    """Steps 2-5 for one target: resolved, contained, repo-relative POSIX, existing file."""
    candidate = target if os.path.isabs(target) else os.path.join(root, target)
    try:
        resolved = os.path.realpath(candidate)
    except OSError:  # pragma: no cover - defensive
        return None
    if resolved != root and not resolved.startswith(root + os.sep):
        return None
    if not os.path.isfile(resolved):
        return None
    rel = PurePath(os.path.relpath(resolved, root)).as_posix()
    return rel or None


def read_set_dependency_paths(read_set: Sequence[str], *, base: str) -> list[str]:
    """The dependency entries a NO-``file_impact`` attestation adds: the normalized read-set
    plus the blast radius. Cited paths are unioned by the caller (they are already part of
    ADR 0002's set for every ticket, scoped or not)."""
    out = {str(path) for path in read_set if str(path)}
    out.update(blast_radius_paths(base=base))
    return sorted(out)


#: The Terraform capture suffixes a synthetic membership glob may range over (REB-640).
_TERRAFORM_GLOB_SUFFIXES = (".tf", ".tf.json")


def terraform_membership_entries(globs: Iterable[str]) -> list[str]:
    """Validate + normalize synthetic Terraform membership globs into dependency entries.

    A Terraform grounding session reports the membership globs (e.g. ``infra/**/*.tf``,
    ``**/*.tf.json``) that bound the set of captures a refutation query DEPENDED ON. Recorded
    as read-set dependency entries they flow through the SAME glob-membership machinery as the
    blast radius (:func:`hash_dep_entry` → :func:`glob_membership_digest`), so a ``.tf`` ADDED
    under the glob after signing moves the attestation's freshness digest — the exact
    "membership freshness" guard the concrete per-file reads cannot provide.

    Each entry must be a repo-relative POSIX glob (contains a metacharacter, is not absolute,
    does not escape the repo with ``..``) that ranges over a Terraform capture suffix.
    Non-conforming entries are dropped (never raised on — a malformed synthetic entry must not
    fail an otherwise-valid attestation). Deduped and sorted for a reproducible signed map."""
    out: set[str] = set()
    for raw in globs or ():
        entry = str(raw or "").strip()
        if not entry or os.path.isabs(entry) or not is_glob(entry):
            continue
        posix = PurePath(entry).as_posix()
        if posix.startswith("../") or "/../" in posix or posix == "..":
            continue
        if not any(posix.endswith(suffix) for suffix in _TERRAFORM_GLOB_SUFFIXES):
            continue
        out.add(posix)
    return sorted(out)
