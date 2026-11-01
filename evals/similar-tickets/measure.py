#!/usr/bin/env python3
"""Duplicate-retrieval benchmark for epic ``62dc-535b-5d05-421d``.

Derives a labeled retrieval set from every recorded ``duplicates`` link in the
store, closes those links transitively into clusters, enumerates within-cluster
pairs, and stratifies each pair:

``lexical``
    The two normalized titles are byte-identical. Overwhelmingly a
    bulk-creation artifact rather than an authored duplicate.
``semantic``
    The titles differ. This is the decision criterion for the architecture
    choice; the lexical stratum is a control every candidate should saturate.

Recall is reported **per stratum, never pooled** -- pooling lets the 94%
lexical majority score any cheap lexical candidate at ~100% and reject vector
search on an artifact.

Modes::

    measure.py --build                 # write benchmark.json from the store
    measure.py --check                 # re-derive and compare against the fixture
    measure.py --build-digests         # fill digests.json (needs model access)
    measure.py --candidate fts5        # lexical baseline
    measure.py --candidate digest      # BM25F over LLM digests
    measure.py --candidate vector      # Titan Text Embeddings V2 + cosine
    measure.py --candidate vector-raw  # diagnostic: same embedding, no LLM digest
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
BENCHMARK = HERE / "benchmark.json"
CORPUS_TEXT = HERE / "corpus-text.json"
# Corpus fields held out of the committed fixture: megabytes of ticket prose,
# rebuilt from the store by --build exactly like the fixture itself.
CORPUS_TEXT_FIELDS = ("description", "digest_input")
DIGESTS = HERE / "digests.json"
#: Cached embedding vectors, keyed by what was embedded. Separate files so the two
#: vector runs never reuse each other's vectors.
VECTORS_DIGEST = HERE / "vectors-digest.json"
VECTORS_RAW = HERE / "vectors-raw.json"

DIGEST_INPUT_LIMIT = 40_000
QUERY_DESC_HEAD = 200
RECALL_KS = (5, 10)

#: BM25F field weights for candidate 2. The one free parameter in this eval;
#: recorded in ADR 0122 and deliberately not tuned per candidate.
DIGEST_FIELD_WEIGHTS = {
    "problem_keywords": 2.0,
    "component_or_area": 1.0,
    "key_entities": 2.0,
    "propositions": 3.0,
}
#: Field order used for both the BM25F document and the embedded paragraph.
DIGEST_FIELDS = ("problem_keywords", "component_or_area", "key_entities", "propositions")

TITAN_MODEL_ID = "amazon.titan-embed-text-v2:0"
TITAN_DIMENSIONS = 1024

#: Prompt and contract are read from the commit that still carried them, the
#: parent of the removal commit 2de5f341ec4. Only the prompt text and output
#: shape are reused; the deleted ``overlap/`` orchestration is NOT restored.
DIGEST_PROMPT_COMMIT = "f147e08242f"
DIGEST_PROMPT_PATH = "src/rebar/llm/reviewers/ticket_digest.md"


# --------------------------------------------------------------------------
# derivation
# --------------------------------------------------------------------------


def normalize_title(title: str) -> str:
    """Fold case, punctuation and whitespace so bulk-created twins collapse."""
    folded = re.sub(r"[^a-z0-9]+", " ", (title or "").lower())
    return " ".join(folded.split())


def digest_input_for(ticket: dict[str, Any]) -> str:
    """Title, description, then every comment oldest-first, blank-line joined."""
    blocks = [ticket.get("title") or "", ticket.get("description") or ""]
    for comment in ticket.get("comments") or []:
        body = (comment or {}).get("body") or ""
        if body:
            blocks.append(body)
    return "\n\n".join(blocks)[:DIGEST_INPUT_LIMIT]


def _union_find(edges: list[tuple[str, str]]) -> dict[str, list[str]]:
    parent: dict[str, str] = {}

    def find(node: str) -> str:
        parent.setdefault(node, node)
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for left, right in edges:
        root_l, root_r = find(left), find(right)
        if root_l != root_r:
            parent[root_l] = root_r

    clusters: dict[str, list[str]] = defaultdict(list)
    for node in parent:
        clusters[find(node)].append(node)
    return clusters


def load_tickets(repo_root: Path | None = None) -> list[dict[str, Any]]:
    """Read every live ticket through the library, not an ad-hoc git read."""
    import rebar

    states = rebar.list_tickets(full=True, exclude_deleted=True, repo_root=repo_root)
    return [s.to_dict() if hasattr(s, "to_dict") else dict(s) for s in states]


def _git(args: list[str], repo_root: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo_root or HERE),
        capture_output=True,
        text=True,
        check=False,
    )


def tickets_head_sha(repo_root: Path | None = None) -> str:
    out = _git(["rev-parse", "origin/tickets"], repo_root)
    if out.returncode != 0:
        sys.exit(f"cannot resolve origin/tickets: {out.stderr.strip()}")
    return out.stdout.strip()


def derive(tickets: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the fixture payload from an in-memory ticket set."""
    by_id = {t["ticket_id"]: t for t in tickets}

    linked: set[frozenset[str]] = set()
    for ticket in tickets:
        for dep in ticket.get("deps") or []:
            if dep.get("relation") != "duplicates":
                continue
            target = dep.get("target_id")
            if target in by_id and target != ticket["ticket_id"]:
                linked.add(frozenset((ticket["ticket_id"], target)))

    edges = [(sorted(pair)[0], sorted(pair)[1]) for pair in linked]
    clusters = [members for members in _union_find(edges).values() if len(members) > 1]

    pairs: list[dict[str, str]] = []
    members_seen: set[str] = set()
    for cluster in clusters:
        members_seen.update(cluster)
        for left, right in itertools.combinations(sorted(cluster), 2):
            same = normalize_title(by_id[left]["title"]) == normalize_title(by_id[right]["title"])
            pairs.append({"a": left, "b": right, "stratum": "lexical" if same else "semantic"})

    corpus: dict[str, dict[str, str]] = {}
    for ticket_id in sorted(members_seen):
        ticket = by_id[ticket_id]
        description = ticket.get("description") or ""
        corpus[ticket_id] = {
            "alias": ticket.get("alias") or "",
            "title": ticket.get("title") or "",
            "description_head": description[:QUERY_DESC_HEAD],
            "description": description,
            "digest_input": digest_input_for(ticket),
        }

    counts: dict[str, int] = defaultdict(int)
    for pair in pairs:
        counts[pair["stratum"]] += 1

    return {
        "duplicates_links": len(linked),
        "clusters": len(clusters),
        "cluster_tickets": len(members_seen),
        "pairs_total": len(pairs),
        "pairs_by_stratum": dict(sorted(counts.items())),
        "pairs": sorted(pairs, key=lambda p: (p["a"], p["b"])),
        "corpus": corpus,
    }


def build(repo_root: Path | None = None) -> dict[str, Any]:
    derived = derive(load_tickets(repo_root))
    fixture = {
        "header": {
            "tickets_sha": tickets_head_sha(repo_root),
            "digest_input_limit": DIGEST_INPUT_LIMIT,
            "query_description_head": QUERY_DESC_HEAD,
            "duplicates_links": derived["duplicates_links"],
            "clusters": derived["clusters"],
            "cluster_tickets": derived["cluster_tickets"],
            "pairs_total": derived["pairs_total"],
            "pairs_by_stratum": derived["pairs_by_stratum"],
            "digest_field_weights": DIGEST_FIELD_WEIGHTS,
            "digest_prompt_commit": DIGEST_PROMPT_COMMIT,
            "embedding_model": TITAN_MODEL_ID,
            "embedding_dimensions": TITAN_DIMENSIONS,
        },
        "pairs": derived["pairs"],
        "corpus": {
            ticket_id: {k: v for k, v in entry.items() if k not in CORPUS_TEXT_FIELDS}
            for ticket_id, entry in derived["corpus"].items()
        },
    }
    BENCHMARK.write_text(json.dumps(fixture, indent=1, sort_keys=True) + "\n")
    CORPUS_TEXT.write_text(
        json.dumps(
            {
                ticket_id: {k: entry[k] for k in CORPUS_TEXT_FIELDS}
                for ticket_id, entry in derived["corpus"].items()
            },
            indent=1,
            sort_keys=True,
        )
        + "\n"
    )
    return fixture


def load_fixture(with_text: bool = False) -> dict[str, Any]:
    """Load the committed fixture, optionally merging the uncommitted text cache.

    The fixture carries only what identifies a pair (ids, aliases, titles,
    description heads). Full descriptions and digest inputs are several MB, are
    rebuilt from the store by ``--build`` along with everything else, and so live
    in an uncommitted sidecar rather than in git. ``--check`` re-derives from the
    store and never needs them.
    """
    if not BENCHMARK.exists():
        sys.exit(f"fixture missing: {BENCHMARK} (run --build)")
    fixture = json.loads(BENCHMARK.read_text())
    if not with_text:
        return fixture
    if not CORPUS_TEXT.exists():
        sys.exit(f"corpus text cache missing: {CORPUS_TEXT} (run --build)")
    text = json.loads(CORPUS_TEXT.read_text())
    missing = [tid for tid in fixture["corpus"] if tid not in text]
    if missing:
        sys.exit(f"corpus text cache is stale: {len(missing)} tickets absent (run --build)")
    for ticket_id, entry in fixture["corpus"].items():
        entry.update(text[ticket_id])
    return fixture


def _compare(fixture: dict[str, Any], derived: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for field in ("duplicates_links", "clusters", "cluster_tickets", "pairs_total"):
        if derived[field] != fixture["header"][field]:
            problems.append(
                f"{field}: fixture {fixture['header'][field]} != store {derived[field]}"
            )
    if derived["pairs_by_stratum"] != fixture["header"]["pairs_by_stratum"]:
        problems.append(
            f"pairs_by_stratum: fixture {fixture['header']['pairs_by_stratum']} "
            f"!= store {derived['pairs_by_stratum']}"
        )
    fixture_pairs = {(p["a"], p["b"], p["stratum"]) for p in fixture["pairs"]}
    store_pairs = {(p["a"], p["b"], p["stratum"]) for p in derived["pairs"]}
    if fixture_pairs != store_pairs:
        problems.append(
            f"pair set differs: {len(fixture_pairs - store_pairs)} only in fixture, "
            f"{len(store_pairs - fixture_pairs)} only in store"
        )
    missing = [tid for tid in fixture["corpus"] if tid not in derived["corpus"]]
    if missing:
        problems.append(f"{len(missing)} fixture tickets no longer resolve in the store")
    return problems


def check(repo_root: Path | None = None) -> int:
    """Re-derive from the CURRENT store and fail loudly on any drift.

    The recorded SHA is used as an ancestry assertion, not a checkout point:
    derivation reads the store as it is now, and the recorded SHA must be an
    ancestor of the current tickets HEAD. So a pass means the committed fixture
    is still true of today's store, not that it was replayed at that SHA.
    """
    fixture = load_fixture()
    recorded = fixture["header"]["tickets_sha"]
    head = tickets_head_sha(repo_root)
    if _git(["merge-base", "--is-ancestor", recorded, head], repo_root).returncode != 0:
        print(
            f"FAIL: recorded tickets SHA {recorded[:11]} is not an ancestor of current "
            f"tickets HEAD {head[:11]}; the fixture is stale.",
            file=sys.stderr,
        )
        return 1

    problems = _compare(fixture, derive(load_tickets(repo_root)))
    if problems:
        for line in problems:
            print(f"FAIL: {line}", file=sys.stderr)
        return 1
    print(
        f"OK: {fixture['header']['pairs_total']} pairs "
        f"({fixture['header']['pairs_by_stratum']}) over "
        f"{fixture['header']['cluster_tickets']} tickets re-derive exactly at {recorded[:11]}"
    )
    return 0


# --------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------


def query_text(entry: dict[str, Any]) -> str:
    return f"{entry['title']} {entry['description_head']}".strip()


def recall_table(fixture: dict[str, Any], ranker, label: str) -> dict[str, Any]:
    """Score every ordered (query, target) direction of every pair."""
    directed: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for pair in fixture["pairs"]:
        directed[pair["stratum"]].append((pair["a"], pair["b"]))
        directed[pair["stratum"]].append((pair["b"], pair["a"]))

    cache: dict[str, list[str]] = {}
    rows: dict[str, Any] = {}
    for stratum, queries in sorted(directed.items()):
        hits = dict.fromkeys(RECALL_KS, 0)
        for source, target in queries:
            if source not in cache:
                cache[source] = ranker(source)
            ranked = cache[source]
            for k in RECALL_KS:
                if target in ranked[:k]:
                    hits[k] += 1
        rows[stratum] = {
            "directed_queries": len(queries),
            **{f"recall@{k}": (hits[k] / len(queries) if queries else 0.0) for k in RECALL_KS},
        }
    return {"candidate": label, "strata": rows}


def print_table(results: dict[str, Any]) -> None:
    print(f"\ncandidate: {results['candidate']}")
    header = " ".join(f"{'recall@' + str(k):>10}" for k in RECALL_KS)
    print(f"{'stratum':<10} {'queries':>8} {header}")
    for stratum, row in sorted(results["strata"].items()):
        cells = " ".join(f"{row[f'recall@{k}']:>10.3f}" for k in RECALL_KS)
        print(f"{stratum:<10} {row['directed_queries']:>8} {cells}")
    print("\n(strata are reported separately; a pooled figure is not meaningful here)")


# --------------------------------------------------------------------------
# candidate 1 -- lexical baseline
# --------------------------------------------------------------------------


def _fts_query(text: str) -> str:
    tokens = re.findall(r"[A-Za-z0-9_]+", text)
    return " OR ".join(f'"{t}"' for t in tokens[:64]) or '"zzzz_no_match"'


def _ranker_from_fts(conn: sqlite3.Connection, fixture: dict[str, Any], weights: str):
    def ranker(source: str) -> list[str]:
        query = _fts_query(query_text(fixture["corpus"][source]))
        rows = conn.execute(
            f"SELECT ticket_id FROM docs WHERE docs MATCH ? "
            f"ORDER BY bm25(docs, {weights}) LIMIT ?",
            (query, max(RECALL_KS) + 1),
        ).fetchall()
        return [row[0] for row in rows if row[0] != source]

    return ranker


def rank_fts5(fixture: dict[str, Any]):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE VIRTUAL TABLE docs USING fts5(ticket_id UNINDEXED, title, description)")
    conn.executemany(
        "INSERT INTO docs (ticket_id, title, description) VALUES (?, ?, ?)",
        [(tid, e["title"], e["description"]) for tid, e in fixture["corpus"].items()],
    )
    return _ranker_from_fts(conn, fixture, "0.0, 5.0, 1.0")


# --------------------------------------------------------------------------
# candidates 2 and 3 -- LLM digests
# --------------------------------------------------------------------------


def digest_prompt_text(repo_root: Path | None = None) -> str:
    out = _git(["show", f"{DIGEST_PROMPT_COMMIT}:{DIGEST_PROMPT_PATH}"], repo_root)
    if out.returncode != 0:
        sys.exit(f"cannot read the retired digest prompt: {out.stderr.strip()}")
    return out.stdout


def digest_key(digest_input: str, prompt_text: str) -> str:
    sha = hashlib.sha256()
    sha.update(digest_input.encode())
    sha.update(b"\x00")
    sha.update(prompt_text.encode())
    return sha.hexdigest()


def load_digests() -> dict[str, Any]:
    if DIGESTS.exists():
        return json.loads(DIGESTS.read_text())
    return {"model": "", "digests": {}}


def digest_document(digest: dict[str, Any]) -> dict[str, str]:
    doc: dict[str, str] = {}
    for field in DIGEST_FIELDS:
        value = digest.get(field)
        if value is None:
            value = "" if field == "component_or_area" else []
        doc[field] = value if isinstance(value, str) else " ".join(value)
    return doc


def digest_paragraph(digest: dict[str, Any]) -> str:
    doc = digest_document(digest)
    return "\n".join(f"{field}: {doc[field]}" for field in DIGEST_FIELDS)[:DIGEST_INPUT_LIMIT]


def rank_digest_bm25f(fixture: dict[str, Any], digests: dict[str, Any]):
    """BM25F over the four digest fields as weighted FTS5 columns."""
    conn = sqlite3.connect(":memory:")
    cols = ", ".join(DIGEST_FIELDS)
    placeholders = ", ".join("?" * len(DIGEST_FIELDS))
    conn.execute(f"CREATE VIRTUAL TABLE docs USING fts5(ticket_id UNINDEXED, {cols})")
    rows = []
    for tid in fixture["corpus"]:
        record = (digests.get("digests") or {}).get(tid)
        if not record:
            continue
        doc = digest_document(record.get("value", record))
        rows.append((tid, *(doc[field] for field in DIGEST_FIELDS)))
    conn.executemany(
        f"INSERT INTO docs (ticket_id, {cols}) VALUES (?, {placeholders})", rows
    )
    weights = "0.0, " + ", ".join(str(DIGEST_FIELD_WEIGHTS[f]) for f in DIGEST_FIELDS)
    return _ranker_from_fts(conn, fixture, weights)


def rank_vector(fixture: dict[str, Any], vectors: dict[str, list[float]]):
    ids = sorted(vectors)

    def ranker(source: str) -> list[str]:
        base = vectors.get(source)
        if base is None:
            return []
        scored = [
            (sum(a * b for a, b in zip(base, vectors[other], strict=True)), other)
            for other in ids
            if other != source
        ]
        scored.sort(reverse=True)
        return [tid for _, tid in scored[: max(RECALL_KS)]]

    return ranker


def embed_texts(
    texts: dict[str, str], region: str, cache: Path | None = None
) -> tuple[dict[str, list[float]], str]:
    """Embed arbitrary per-ticket text. Returns (vectors, unavailable_reason).

    Split out of ``embed_corpus`` so the same Titan call can embed either the LLM
    digest paragraph or the raw ticket text, which is what isolates whether the
    digest step earns its per-ticket model call.

    The cache stores ``{tid: {"fingerprint": sha256(text), "embedding": [...]}}``
    and reuses an entry only when the fingerprint of the text about to be embedded
    matches, so regenerating ``digests.json`` cannot silently serve vectors of the
    superseded text. A legacy cache (bare vector lists, no fingerprint) is ignored
    and re-embedded. Partial progress is flushed before returning a failure, so an
    interrupted run does not discard the calls it already paid for.
    """
    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError as exc:
        return {}, f"boto3 unavailable: {exc}"

    vectors: dict[str, list[float]] = {}
    cached: dict[str, dict[str, Any]] = {}
    if cache is not None and cache.exists():
        raw = json.loads(cache.read_text())
        for tid, entry in raw.items():
            if isinstance(entry, dict) and "fingerprint" in entry:
                cached[tid] = entry
    client = boto3.client("bedrock-runtime", region_name=region)

    def flush() -> None:
        if cache is not None:
            cache.write_text(json.dumps(cached))

    for tid in sorted(texts):
        payload = texts[tid][:DIGEST_INPUT_LIMIT]
        fingerprint = hashlib.sha256(payload.encode()).hexdigest()
        entry = cached.get(tid)
        if entry is not None and entry["fingerprint"] == fingerprint:
            vectors[tid] = entry["embedding"]
            continue
        body = json.dumps(
            {
                "inputText": payload,
                "dimensions": TITAN_DIMENSIONS,
                "normalize": True,
            }
        )
        try:
            resp = client.invoke_model(modelId=TITAN_MODEL_ID, body=body)
        except ClientError as exc:
            flush()
            code = exc.response.get("Error", {}).get("Code", "")
            return {}, f"{code or 'ClientError'}: {exc}"
        except (BotoCoreError, OSError) as exc:
            flush()
            return {}, f"credentials or transport unavailable: {exc}"
        embedding = json.loads(resp["body"].read())["embedding"]
        vectors[tid] = embedding
        cached[tid] = {"fingerprint": fingerprint, "embedding": embedding}
    flush()
    return vectors, ""


def embed_corpus(
    fixture: dict[str, Any], digests: dict[str, Any], region: str
) -> tuple[dict[str, list[float]], str]:
    """Return (vectors, unavailable_reason). Never aborts the run."""
    texts = {}
    for tid in sorted(fixture["corpus"]):
        record = (digests.get("digests") or {}).get(tid)
        if record:
            texts[tid] = digest_paragraph(record.get("value", record))
    return embed_texts(texts, region, VECTORS_DIGEST)


def embed_corpus_raw(fixture: dict[str, Any], region: str) -> tuple[dict[str, list[float]], str]:
    """Embed the same raw title+description FTS5 indexes, with no LLM digest step."""
    texts = {
        tid: f"{entry['title']}\n{entry['description']}"
        for tid, entry in fixture["corpus"].items()
    }
    return embed_texts(texts, region, VECTORS_RAW)


def build_digests(repo_root: Path | None = None) -> int:
    """Fill digests.json, calling the model only on a cache miss.

    The digest prompt and its ``TicketDigest`` contract were deleted with the v1
    retirement, so neither is packaged any more: the prompt text is recovered from
    git (``digest_prompt_text``) and passed as the system prompt directly, and the
    4 fields are parsed out of a plain-text JSON reply. That keeps the measurement
    honest — it reruns the retired v1 digest — without restoring deleted machinery.
    """
    import dataclasses

    from rebar.llm import model_classes
    from rebar.llm.config import LLMConfig
    from rebar.llm.runner import get_runner

    root = str(repo_root) if repo_root else str(REPO_ROOT)
    fixture = load_fixture(with_text=True)
    prompt = digest_prompt_text(repo_root)
    store = load_digests()
    cached = store.get("digests") or {}
    model = model_classes.resolve_model_string("trivial", root)
    store["model"] = model

    cfg = dataclasses.replace(LLMConfig.from_env(repo_root=root), model=model)
    runner = get_runner(cfg)
    runner.preflight()

    misses = 0
    empties = 0
    for tid, entry in sorted(fixture["corpus"].items()):
        fingerprint = digest_key(entry["digest_input"], prompt)
        if cached.get(tid, {}).get("fingerprint") == fingerprint:
            continue
        misses += 1
        value = _call_digest(runner, cfg, prompt, entry["digest_input"])
        if not any(value.get(field) for field in DIGEST_FIELDS):
            empties += 1
        cached[tid] = {"fingerprint": fingerprint, "value": value}
        if misses % 25 == 0:
            snapshot = store | {"digests": cached}
            DIGESTS.write_text(json.dumps(snapshot, indent=1, sort_keys=True) + "\n")
            print(f"  … {misses} calls, {empties} empty", flush=True)
    store["digests"] = cached
    DIGESTS.write_text(json.dumps(store, indent=1, sort_keys=True) + "\n")
    print(f"digests: {len(cached)} cached, {misses} model calls, {empties} empty, model={model}")
    return 0


def _call_digest(runner: Any, cfg: Any, prompt: str, digest_input: str) -> dict[str, Any]:
    """One single-turn text call per ticket, parsed into the 4 TicketDigest fields.

    ``mode="text"`` + ``execution_mode="single_turn"`` is the one shape that runs a
    toolless single call without a packaged output schema (the digest's schema was
    deleted); the reply is a JSON object, so it is parsed here.
    """
    from rebar.llm.runner import RunRequest

    req = RunRequest(
        system_prompt=prompt,
        instructions=digest_input,
        config=cfg,
        reviewers=["ticket-digest"],
        mode="text",
        execution_mode="single_turn",
    )
    text = str(runner.run(req).get("text", "")).strip()
    return _parse_digest_reply(text)


def _digest_reply_candidates(text: str) -> list[str]:
    """Every substring of a reply that might be the digest object, best first.

    Three reply shapes were measured over the 630-ticket corpus and each defeats a
    naive parse in its own way: a bare object; a fenced block behind a prose
    preamble (``# Ticket Digest\\n\\n```json ...``), which a start-anchored fence
    strip never sees; and YAML instead of JSON. Returning candidates rather than
    one cleaned string keeps the recovery rules in one place.
    """
    out = [text]
    out.extend(m.group(1).strip() for m in re.finditer(r"```[a-zA-Z]*\n(.*?)\n```", text, re.S))
    first, last = text.find("{"), text.rfind("}")
    if first != -1 and last > first:
        out.append(text[first : last + 1])
    return out


def _first_mapping(candidates: list[str]) -> dict[str, Any] | None:
    """First candidate that parses as a mapping, trying JSON then YAML.

    YAML is a JSON superset, so the second parse costs nothing on well-formed JSON
    and recovers the replies that answer in YAML (measured: 19 of the first 375).
    """
    for candidate in candidates:
        if not candidate:
            continue
        for parse in (json.loads, _safe_yaml):
            try:
                value = parse(candidate)
            except Exception:  # noqa: BLE001 - any parse failure just means "try the next candidate"
                continue
            if isinstance(value, dict):
                return value
    return None


def _safe_yaml(text: str) -> Any:
    import yaml

    return yaml.safe_load(text)


def _parse_digest_reply(text: str) -> dict[str, Any]:
    """Normalize a digest reply to exactly the 4 fields, each a string.

    A missing or unparseable reply yields empty fields rather than raising: one bad
    ticket must not abort a 630-ticket corpus build, and an empty digest simply
    retrieves nothing, which the measurement then reports honestly.
    """
    raw = _first_mapping(_digest_reply_candidates(text))
    if raw is None:
        return {field: "" for field in DIGEST_FIELDS}
    # The retired contract was a named model, so the reply nests the 4 fields under a
    # `ticket_digest` wrapper. Descend through single-key wrappers until the fields appear.
    for _ in range(3):
        if any(field in raw for field in DIGEST_FIELDS):
            break
        if len(raw) != 1:
            break
        inner = next(iter(raw.values()))
        if not isinstance(inner, dict):
            break
        raw = inner
    out: dict[str, Any] = {}
    for field in DIGEST_FIELDS:
        value = raw.get(field, "")
        out[field] = " ".join(str(v) for v in value) if isinstance(value, list) else str(value)
    return out


# --------------------------------------------------------------------------


def run_candidate(name: str) -> int:
    fixture = load_fixture(with_text=True)
    region = os.environ.get("REBAR_BEDROCK_REGION", "us-east-1")
    if name == "fts5":
        label = "fts5 (bm25 title 5.0 / description 1.0)"
        print_table(recall_table(fixture, rank_fts5(fixture), label))
        return 0

    if name == "vector-raw":
        # Diagnostic, not one of the three plan candidates: embeds the same raw
        # text fts5 indexes, so the gap against `vector` is exactly what the LLM
        # digest contributes, and that decides whether the digest's per-ticket
        # model call belongs in the recommended architecture.
        vectors, reason = embed_corpus_raw(fixture, region)
        if reason:
            print(f"candidate vector-raw unavailable: {reason}", file=sys.stderr)
            return 3
        label = f"vector over RAW title+description ({TITAN_MODEL_ID})"
        print_table(recall_table(fixture, rank_vector(fixture, vectors), label))
        return 0

    digests = load_digests()
    if not (digests.get("digests") or {}):
        print(
            f"candidate {name} unavailable: no digests in {DIGESTS.name}; "
            "run --build-digests with model access first.",
            file=sys.stderr,
        )
        return 3

    if name == "digest":
        print_table(recall_table(fixture, rank_digest_bm25f(fixture, digests), "digest BM25F"))
        return 0

    vectors, reason = embed_corpus(fixture, digests, region)
    if reason:
        print(f"candidate vector unavailable: {reason}", file=sys.stderr)
        print("An unavailable candidate MUST NOT be selected by the ADR.", file=sys.stderr)
        return 3
    print_table(recall_table(fixture, rank_vector(fixture, vectors), f"vector ({TITAN_MODEL_ID})"))
    return 0


def _hit_vectors(fixture: dict[str, Any], ranker, k: int) -> dict[str, list[int]]:
    """Per-directed-query hit/miss at k, keyed by stratum — the paired-test input."""
    directed: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for pair in fixture["pairs"]:
        directed[pair["stratum"]].append((pair["a"], pair["b"]))
        directed[pair["stratum"]].append((pair["b"], pair["a"]))

    cache: dict[str, list[str]] = {}
    out: dict[str, list[int]] = {}
    for stratum, queries in sorted(directed.items()):
        hits = []
        for source, target in queries:
            if source not in cache:
                cache[source] = ranker(source)
            hits.append(1 if target in cache[source][:k] else 0)
        out[stratum] = hits
    return out


def _mcnemar(a: list[int], b: list[int]) -> dict[str, Any]:
    """Exact two-sided McNemar on paired hit vectors.

    Only the discordant queries carry information: those `a` hit and `b` missed
    (b01) against those `b` hit and `a` missed (b10). Under the null the split is
    Binomial(n, 0.5), so the exact p needs no normal approximation — which matters
    here because the semantic stratum has few queries.
    """
    b01 = sum(1 for x, y in zip(a, b, strict=True) if x and not y)
    b10 = sum(1 for x, y in zip(a, b, strict=True) if y and not x)
    n = b01 + b10
    if n == 0:
        return {"b01": 0, "b10": 0, "p": 1.0}
    lo = min(b01, b10)
    tail = sum(math.comb(n, i) for i in range(lo + 1)) / (2**n)
    return {"b01": b01, "b10": b10, "p": min(1.0, 2 * tail)}


def run_paired(k: int = 5) -> int:
    """Pairwise McNemar between every candidate, per stratum, at recall@k.

    A recall difference read off two separate tables can sit inside the noise of a
    186-query stratum. The candidates answer the SAME queries, so the paired test
    is the one that can tell a real ordering from sampling noise, and it is what
    an architecture recommendation has to rest on.
    """
    fixture = load_fixture(with_text=True)
    region = os.environ.get("REBAR_BEDROCK_REGION", "us-east-1")
    digests = load_digests()

    rankers: dict[str, Any] = {"fts5": rank_fts5(fixture)}
    if digests.get("digests"):
        rankers["digest"] = rank_digest_bm25f(fixture, digests)
        vecs, reason = embed_corpus(fixture, digests, region)
        if not reason:
            rankers["vector"] = rank_vector(fixture, vecs)
    raw_vecs, reason = embed_corpus_raw(fixture, region)
    if not reason:
        rankers["vector-raw"] = rank_vector(fixture, raw_vecs)

    hits = {name: _hit_vectors(fixture, r, k) for name, r in rankers.items()}
    names = list(hits)
    strata = sorted(next(iter(hits.values())))

    print(f"\npaired McNemar at recall@{k} (exact, two-sided)")
    for stratum in strata:
        n = len(hits[names[0]][stratum])
        print(f"\n  stratum {stratum} ({n} directed queries)")
        print(f"  {'A vs B':<28} {'A-only':>7} {'B-only':>7} {'p':>9}  verdict")
        for i, a in enumerate(names):
            for b in names[i + 1 :]:
                r = _mcnemar(hits[a][stratum], hits[b][stratum])
                verdict = "distinguishable" if r["p"] < 0.05 else "NOT distinguishable"
                print(
                    f"  {a + ' vs ' + b:<28} {r['b01']:>7} {r['b10']:>7} "
                    f"{r['p']:>9.4f}  {verdict}"
                )
    print("\n(A-only = queries A ranked in top-k and B did not;")
    print(" concordant queries carry no signal)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="duplicate-retrieval benchmark")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--build", action="store_true", help="write benchmark.json from the store")
    group.add_argument("--check", action="store_true", help="re-derive and compare")
    group.add_argument("--build-digests", action="store_true", help="fill digests.json")
    group.add_argument("--candidate", choices=("fts5", "digest", "vector", "vector-raw"))
    group.add_argument(
        "--paired",
        action="store_true",
        help="pairwise McNemar between candidates, per stratum",
    )
    parser.add_argument(
        "--k", type=int, default=5, help="rank cutoff for --paired (default 5)"
    )
    args = parser.parse_args(argv)

    if args.build:
        print(json.dumps(build()["header"], indent=2))
        return 0
    if args.check:
        return check()
    if args.build_digests:
        return build_digests()
    if args.paired:
        return run_paired(args.k)
    return run_candidate(args.candidate)


if __name__ == "__main__":
    raise SystemExit(main())
