#!/usr/bin/env python3
"""Build the frozen, sanitized Cloud Markdown↔ADF fixture corpus.

Sample real ticket prose deterministically, normalize vocabulary, scrub secrets,
and shard under the 500 KB added-file cap. ``test_adf_markdown.py`` checks the
vendored round-trip corpus without a live store. Read the local store and write
only ``tests/fixtures/cloud_adf_corpus/`` by default:

    python scripts/build_cloud_adf_corpus.py [--store PATH] [--out PATH]

Regenerate only for an intentional corpus refresh; update asserted counts in
the same change."""

from __future__ import annotations

import argparse
import json
import random
import re
from pathlib import Path
from typing import Any

# Deterministic sampling so a re-run without a store change reproduces the corpus.
_SEED = 20260810
# Bodies per shard, and the per-body ceiling that keeps a single outlier from
# blowing a shard past the 500 KB pre-commit cap.
_SHARDS = 3
_PER_SHARD = 120
_MAX_BODY_CHARS = 12_000

# --- scrubbing (mirrors scripts/capture_jira_fixtures.py) -------------------
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_URL_RE = re.compile(r"https?://[^\s)>\]]+")
# Bare hostname too, not just the URL form: real bodies name tenants inline as
# ``(acme.atlassian.net)`` with no scheme, which a URL-anchored pattern misses.
_TENANT_HOST_RE = re.compile(r"\b[a-z0-9][a-z0-9-]*\.atlassian\.net\b")
_ACCOUNTID_RE = re.compile(r"\b[0-9a-f]{24}\b")
_TOKENISH_RE = re.compile(r"\b[A-Za-z0-9_-]{32,}\b")

# Normalize retired vocabulary in captured prose: it is irrelevant to
# Markdown/ADF behavior, and tracked fixtures must pass vocabulary guards.
_VOCABULARY_SUBSTITUTIONS = (
    ("bridge-status", "bridge status"),
    ("bridge-fsck", "bridge fsck"),
    ("bridge-probe", "bridge check-access"),
    ("jira-onboard", "bridge setup"),
    ("verify-authorship", "verify-identity"),
    ("Success Criteria", "Acceptance Criteria"),
    ("success criteria", "acceptance criteria"),
    ("SUCCESS CRITERIA", "ACCEPTANCE CRITERIA"),
    ("--force-close", '--force="<reason>"'),
)


def scrub(text: str) -> str:
    """Redact emails, hosts, URLs and token-shaped runs from a body."""
    text = _EMAIL_RE.sub("redacted@example.com", text)
    text = _URL_RE.sub("https://example.invalid/redacted", text)
    text = _TENANT_HOST_RE.sub("example.invalid", text)
    text = _ACCOUNTID_RE.sub("0" * 24, text)
    text = _TOKENISH_RE.sub("REDACTED_TOKEN", text)
    for retired, canonical in _VOCABULARY_SUBSTITUTIONS:
        text = text.replace(retired, canonical)
    return text


def _iter_bodies(store: Path) -> list[str]:
    """Yield every live description and comment body in the ticket store."""
    bodies: list[str] = []
    for event in store.glob("*/*.json"):
        name = event.name
        if ".retired" in name:
            continue
        if not (name.endswith("-CREATE.json") or name.endswith("-COMMENT.json")):
            continue
        try:
            payload: Any = json.loads(event.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            continue
        value = data.get("description") if name.endswith("-CREATE.json") else data.get("body")
        if isinstance(value, str) and value.strip():
            bodies.append(value)
    return bodies


def build(store: Path, out: Path) -> dict[str, int]:
    seen: set[str] = set()
    unique: list[str] = []
    for body in _iter_bodies(store):
        if len(body) <= _MAX_BODY_CHARS and body not in seen:
            seen.add(body)
            unique.append(body)
    unique.sort()  # stable order before sampling

    rng = random.Random(_SEED)
    wanted = _SHARDS * _PER_SHARD
    chosen = sorted(rng.sample(unique, wanted)) if len(unique) > wanted else unique

    out.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    for index in range(_SHARDS):
        shard = [scrub(b) for b in chosen[index * _PER_SHARD : (index + 1) * _PER_SHARD]]
        name = f"bodies_{index:02d}"
        (out / f"{name}.json").write_text(
            json.dumps(shard, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        counts[name] = len(shard)
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    repo = Path(__file__).resolve().parent.parent
    parser.add_argument("--store", type=Path, default=repo / ".tickets-tracker")
    default_out = repo / "tests" / "fixtures" / "cloud_adf_corpus"
    parser.add_argument("--out", type=Path, default=default_out)
    args = parser.parse_args()

    counts = build(args.store, args.out)
    for name, count in sorted(counts.items()):
        size = (args.out / f"{name}.json").stat().st_size
        print(f"{name}: {count} bodies, {size / 1024:.1f} KB")


if __name__ == "__main__":
    main()
