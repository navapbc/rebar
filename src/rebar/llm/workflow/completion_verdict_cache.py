"""Cross-run PASS-verdict cache for the completion verifier (ticket 8d74-2c9f-c98f-4f0b).

A re-verify after exhaustion used to re-prove EVERY criterion from scratch. This module
persists each VALIDATED PASS record at finalize under
``.rebar/cache/completion_verdicts/<ticket>/<criterion-hash>.json`` and seeds still-valid
entries into the next run's run-scoped :class:`~.completion_banking.CriterionBank`, stamped
``seeded: true``. The run-scoped bank itself is unchanged (its run-unique dir is the
concurrent-close isolation guarantee); this cache is the cross-run layer above it.

Design pins (all review-certified in the ticket plan):

* **PASS-only.** Entries are written only for records the merged, coverage-validated verdict
  scored ``met=true`` AND that are backed by a bank entry without the
  ``evidence_sufficient=False`` marker. Insufficiency/FAIL is NEVER cached — the cache can
  only credit what an earlier validated run actually proved.
* **Scoped content fingerprint, not the whole-repo tree.** ``BankStamps.material_fingerprint``
  hashes plan TEXT and ``tree_sha`` rotates on any unrelated commit, so both are unusable as
  the reuse key. Each entry is keyed by (criterion-text hash, scoped fingerprint) where the
  fingerprint hashes the git BLOB shas of the ticket's ``file_impact`` paths: an in-scope
  edit rotates a blob and invalidates; an unrelated commit does not. Absent path → sentinel;
  git failure → ``None`` (reuse disabled for the run — fail-open to re-verification); empty
  own impact + children → union of DIRECT-child impact blobs; childless + empty → whole-tree
  sha fallback.
* **Atomic writes** (tmp + ``os.replace``), last-write-wins safe across concurrent closes.
* **Best-effort everywhere**: no cache path may fail a verification run.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from rebar._config_sources import repo_root as _resolve_root

from .completion_banking import _MANIFEST_TEXT_CAP
from .completion_prefetch import _normalize_impact

logger = logging.getLogger(__name__)

CACHE_SCHEMA_VERSION = 1
_ABSENT_BLOB_SENTINEL = "absent"
_GIT_TIMEOUT_SECONDS = 30


def criterion_cache_key(text: str) -> str:
    """The cache filename key: sha256 of the criterion text normalized exactly as
    ``mint_criterion_id`` normalizes it, WITHOUT the positional index — a criterion that
    merely moved within the plan keeps its cached verdict."""
    norm = re.sub(r"\s+", " ", str(text)).strip().casefold()
    return hashlib.sha256(norm.encode()).hexdigest()[:16]


def cache_dir(repo_root: str | None, ticket_id: str) -> Path:
    """``None`` resolves through the canonical root resolver (REBAR_ROOT > git toplevel),
    NEVER the bare cwd — the cache must land in the store's checkout."""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(ticket_id)) or "unknown"
    return _resolve_root(repo_root) / ".rebar" / "cache" / "completion_verdicts" / safe


# ── ticket-graph reads (fail-open: enumeration must never raise) ────────────────────
def direct_children(ticket_id: str, repo_root: str | None) -> list[dict[str, Any]]:
    """The ticket's DIRECT children — the same list budget sizing uses. Fail-open to []."""
    try:
        from rebar import _reads

        kids = _reads.list_tickets(parent=ticket_id, repo_root=repo_root)
        return [kid for kid in kids if isinstance(kid, dict)]
    except Exception:  # noqa: BLE001 -- sizing/seeding sites must never fail on enumeration
        return []


def direct_child_count(ticket_id: str, repo_root: str | None) -> int:
    return len(direct_children(ticket_id, repo_root))


# ── scoped content fingerprint ──────────────────────────────────────────────────────
def _rev_parse_lines(repo_root: str | None, *specs: str) -> list[str] | None:
    """Read-only ``git rev-parse`` (literal subcommand: statically provable non-write)."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", *specs],
            cwd=_resolve_root(repo_root),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.splitlines()


def _blob_shas(repo_root: str | None, paths: list[str]) -> list[str] | None:
    """Resolve each path to its HEAD blob sha; absent path → sentinel; git broken → None.

    One batched ``git rev-parse`` first; only when the batch fails (ANY absent path fails
    the whole batch) does it fall back to per-path calls, where a failure means that ONE
    path is absent from HEAD (the git-health probe already ran)."""
    batched = _rev_parse_lines(repo_root, *[f"HEAD:{path}" for path in paths])
    if batched is not None and len(batched) == len(paths):
        return [line.strip() for line in batched]
    shas: list[str] = []
    for path in paths:
        lines = _rev_parse_lines(repo_root, f"HEAD:{path}")
        shas.append(lines[0].strip() if lines else _ABSENT_BLOB_SENTINEL)
    return shas


def scoped_content_fingerprint(
    ticket: dict[str, Any], children: list[dict[str, Any]], repo_root: str | None
) -> str | None:
    """The reuse key's content half; ``None`` disables reuse for this run (fail-open)."""
    tree_lines = _rev_parse_lines(repo_root, "HEAD^{tree}")  # git-health probe
    if not tree_lines:
        return None
    paths = _normalize_impact(ticket.get("file_impact"))
    if not paths:
        child_paths = sorted(
            {p for kid in children for p in _normalize_impact(kid.get("file_impact"))}
        )
        if child_paths:
            paths = child_paths
        elif children:
            return None  # childful with NO impact surface anywhere: no meaningful scope
        else:
            return f"tree:{tree_lines[0].strip()}"  # childless + empty impact → whole tree
    ordered = sorted(set(paths))
    shas = _blob_shas(repo_root, ordered)
    if shas is None:
        return None
    material = "\n".join(f"{path}\0{sha}" for path, sha in zip(ordered, shas, strict=True))
    return f"blobs:{hashlib.sha256(material.encode()).hexdigest()[:32]}"


# ── persist (validated PASS at finalize) ────────────────────────────────────────────
def persist_pass_verdicts(
    ticket_id: str,
    verdict: dict[str, Any],
    bank_entries: dict[str, dict[str, Any]],
    repo_root: str | None,
) -> int:
    """Write the merged verdict's validated-PASS records to the cross-run cache.

    A record is cached only when the MERGED (coverage-validated) verdict scored it
    ``met=true`` and its bank entry agrees (``met=true``, no insufficiency marker) — the
    bank entry is what gets stored, so a seeded entry simply refreshes itself. Best-effort:
    returns the count written, never raises."""
    try:
        passing = [
            record
            for record in verdict.get("criteria") or []
            if isinstance(record, dict)
            and record.get("met") is True
            and not record.get("unverified")
            and record.get("criterion")
        ]
        if not passing:
            return 0
        from rebar import _reads

        ticket = _reads.show_ticket(ticket_id, repo_root=repo_root)
        fingerprint = scoped_content_fingerprint(
            ticket, direct_children(ticket_id, repo_root), repo_root
        )
        if fingerprint is None:
            return 0
        directory = cache_dir(repo_root, ticket_id)
        directory.mkdir(parents=True, exist_ok=True)
        written = 0
        for record in passing:
            entry = bank_entries.get(str(record.get("criterion_id") or ""))
            if not isinstance(entry, dict) or entry.get("met") is not True:
                continue  # PASS-only AND bank-backed: never cache finalizer-only credit
            if entry.get("evidence_sufficient") is False:
                continue
            text = str(record["criterion"])
            payload = {
                "schema_version": CACHE_SCHEMA_VERSION,
                "ticket_id": str(ticket_id),
                "criterion": text,
                "criterion_text_hash": criterion_cache_key(text),
                "met": True,
                "evidence": str(entry.get("evidence") or ""),
                "truncated": bool(entry.get("truncated")),
                "fingerprint": fingerprint,
            }
            tmp = directory / f"{criterion_cache_key(text)}.tmp"
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8"
            )
            os.replace(tmp, directory / f"{criterion_cache_key(text)}.json")
            written += 1
        return written
    except Exception:  # the cache is an accelerator; it must never fail a close
        logger.debug("completion verdict-cache persist skipped", exc_info=True)
        return 0


# ── load + seed (run start) ─────────────────────────────────────────────────────────
def load_valid_pass_entries(
    ticket_id: str, ticket: dict[str, Any], expected: list[str], repo_root: str | None
) -> dict[str, dict[str, Any]]:
    """criterion text → still-valid cached PASS entry (schema, met, text hash, fingerprint
    all current). A ``None`` fingerprint disables reuse wholesale (fail-open)."""
    out: dict[str, dict[str, Any]] = {}
    try:
        directory = cache_dir(repo_root, ticket_id)
        if not expected or not directory.is_dir():
            return out
        fingerprint = scoped_content_fingerprint(
            ticket, direct_children(ticket_id, repo_root), repo_root
        )
        if fingerprint is None:
            return out
        for text in expected:
            key = criterion_cache_key(text)
            path = directory / f"{key}.json"
            try:
                entry = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(entry, dict) or entry.get("schema_version") != CACHE_SCHEMA_VERSION:
                continue
            if entry.get("met") is not True or entry.get("criterion_text_hash") != key:
                continue
            if entry.get("fingerprint") != fingerprint:
                continue
            out[text] = entry
    except Exception:  # reuse is best-effort; any fault re-verifies instead
        logger.debug("completion verdict-cache load skipped", exc_info=True)
    return out


def seed_bank_from_cache(
    bank: Any,
    ticket_id: str,
    ticket: dict[str, Any],
    expected: list[str],
    id_by_text: dict[str, str],
    repo_root: str | None,
) -> frozenset[str]:
    """Seed still-valid cached PASS verdicts into the run bank (stamped ``seeded: true``);
    return the seeded criterion ids. Best-effort: a fault seeds nothing."""
    seeded: set[str] = set()
    try:
        valid = load_valid_pass_entries(ticket_id, ticket, expected, repo_root)
        for text, entry in valid.items():
            cid = id_by_text.get(text)
            if not cid:
                continue
            bank.upsert(cid, True, str(entry.get("evidence") or ""), source="cache", seeded=True)
            seeded.add(cid)
    except Exception:  # seeding is an accelerator, never a failure mode
        logger.debug("completion verdict-cache seeding skipped", exc_info=True)
    if seeded:
        logger.info(
            "completion verifier: seeded %d cached PASS verdict(s) for %s", len(seeded), ticket_id
        )
    return frozenset(seeded)


def seeded_context_block(seeded_texts: list[str], id_by_text: dict[str, str]) -> str:
    """The primary-context directive for seeded criteria. The manifest omission alone is
    data-only; this block INSTRUCTS the model to skip the already-credited criteria."""
    if not seeded_texts:
        return ""
    lines = [
        "",
        "## Already credited — do not re-verify",
        "A prior validated run proved the following criteria and their PASS verdicts are",
        "already credited. Do not re-verify, re-investigate, or report on them:",
    ]
    for text in seeded_texts:
        one_line = re.sub(r"\s+", " ", text).strip()
        if len(one_line) > _MANIFEST_TEXT_CAP:
            one_line = one_line[:_MANIFEST_TEXT_CAP] + "…"
        lines.append(f"- {id_by_text[text]}: {one_line} — already credited — do not re-verify")
    return "\n".join(lines)


__all__ = [
    "CACHE_SCHEMA_VERSION",
    "cache_dir",
    "criterion_cache_key",
    "direct_child_count",
    "direct_children",
    "load_valid_pass_entries",
    "persist_pass_verdicts",
    "scoped_content_fingerprint",
    "seed_bank_from_cache",
    "seeded_context_block",
]
