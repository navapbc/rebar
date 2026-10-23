#!/usr/bin/env python3
"""Build the frozen, sanitized DC wiki-render fixture corpus.

Deterministically sample real prose, including capped strata for code arrows
and ASCII tables, plus random coverage prose. Scrub secrets, normalize text,
and shard under the 500 KB added-file cap. ``test_wiki_render_corpus.py`` uses
the vendored fixture without a live store:

    python scripts/build_dc_wiki_corpus.py [--store PATH] [--out PATH]

Regenerate only for an intentional refresh; update asserted counts together."""

from __future__ import annotations

import argparse
import json
import random
import re
from pathlib import Path
from typing import Any

# Deterministic sampling so a re-run without a store change reproduces the corpus.
_SEED = 20260810

# Pin stratum counts so a growing store cannot change frozen fixture counts;
# caps also keep each shard below the 500 KB added-file limit.
_STRATUM_TARGETS = {"code_arrow": 29, "table": 29, "prose": 120}
_MAX_BODY_CHARS = 8_000

# --- scrubbing (mirrors scripts/capture_jira_fixtures.py) -------------------
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Bare hostname too, not just the URL form: real bodies name tenants inline as
# ``(acme.atlassian.net)`` with no scheme, which a URL-anchored pattern misses.
_TENANT_HOST_RE = re.compile(r"\b[a-z0-9][a-z0-9-]*\.atlassian\.net\b")
_URL_RE = re.compile(r"https?://[^\s)>\]]+")
_ACCOUNTID_RE = re.compile(r"\b[0-9a-f]{24}\b")
_TOKENISH_RE = re.compile(r"\b[A-Za-z0-9_-]{32,}\b")

# Map retired vocabulary to canonical spellings during capture; it is
# irrelevant to Markdown/wiki conversion and tracked fixtures face guards.
_VOCABULARY_SUBSTITUTIONS = (
    ("bridge-status", "bridge status"),
    ("bridge-fsck", "bridge fsck"),
    ("bridge-probe", "bridge check-access"),
    ("jira-onboard", "bridge setup"),
    ("verify-authorship", "verify-identity"),
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


# --- phenomenon detection ---------------------------------------------------
_FENCE_RE = re.compile(r"^\s*(```+|~~~+)", re.M)
_INLINE_CODE_RE = re.compile(r"`+[^`]*`+")
_ARROW_RE = re.compile(r"(->|<-|<->|=>)")
_PIPE_DELIM_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$", re.M)
_BOX_RULE_RE = re.compile(r"^\s*\+[-+=]{2,}\+\s*$", re.M)


def has_code_arrow(body: str) -> bool:
    """True when an ASCII arrow appears inside a fence or an inline code span."""
    for span in _INLINE_CODE_RE.finditer(body):
        if _ARROW_RE.search(span.group(0)):
            return True
    in_fence = False
    for line in body.split("\n"):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence and _ARROW_RE.search(line):
            return True
    return False


def has_table(body: str) -> bool:
    return bool(_PIPE_DELIM_RE.search(body) or _BOX_RULE_RE.search(body))


def _iter_bodies(store: Path) -> list[str]:
    """Yield every live (non-retired) description and comment body in the store."""
    bodies: list[str] = []
    for event in store.glob("*/*.json"):
        name = event.name
        if name.endswith(".retired.json") or ".retired" in name:
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
    bodies = [b for b in _iter_bodies(store) if len(b) <= _MAX_BODY_CHARS]
    # Deduplicate so a repeated boilerplate comment cannot dominate a stratum.
    seen: set[str] = set()
    unique: list[str] = []
    for body in bodies:
        if body not in seen:
            seen.add(body)
            unique.append(body)
    unique.sort()  # stable order before sampling

    rng = random.Random(_SEED)
    code_arrow = [b for b in unique if has_code_arrow(b)]
    tables = [b for b in unique if has_table(b)]
    plain = [b for b in unique if b not in set(code_arrow) | set(tables)]

    def sample(pool: list[str], target: int) -> list[str]:
        if len(pool) <= target:
            return pool
        return sorted(rng.sample(pool, target))

    strata = {
        "code_arrow": sample(code_arrow, _STRATUM_TARGETS["code_arrow"]),
        "table": sample(tables, _STRATUM_TARGETS["table"]),
        "prose": sample(plain, _STRATUM_TARGETS["prose"]),
    }

    out.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    for name, chosen in strata.items():
        scrubbed = [scrub(b) for b in chosen]
        (out / f"{name}.json").write_text(
            json.dumps(scrubbed, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        counts[name] = len(scrubbed)
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    repo = Path(__file__).resolve().parent.parent
    parser.add_argument("--store", type=Path, default=repo.parent / "rebar" / ".tickets-tracker")
    parser.add_argument("--out", type=Path, default=repo / "tests" / "fixtures" / "dc_wiki_corpus")
    args = parser.parse_args()

    counts = build(args.store, args.out)
    for name, count in sorted(counts.items()):
        size = (args.out / f"{name}.json").stat().st_size
        print(f"{name}: {count} bodies, {size / 1024:.1f} KB")


if __name__ == "__main__":
    main()
