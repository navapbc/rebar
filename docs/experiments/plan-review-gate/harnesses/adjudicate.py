#!/usr/bin/env python3
"""E1 adjudication runner (story doctrinal-untruthful-vaquita / e95e).

Labels the finding-adjudication corpus (runs/adjudication_corpus.jsonl, built by
build_adjudication_corpus.py) with two INDEPENDENT LLM raters:

  * Rater A (primary): MODEL_A + adjudicate_rubric_a.md — labels EVERY row; fills
    ``tp_fp`` and ``rater_a``.
  * Rater B (independent): MODEL_B (a DIFFERENT model) + adjudicate_rubric_b.md —
    re-labels a stratified-by-criterion >=50-finding subset BLIND to Rater A; fills
    ``rater_b``.

kappa.py then computes Cohen's kappa on ``rater_a`` vs ``rater_b`` over the subset.

Design notes (E1 advisories honored):
  * deterministic: temperature 0, fixed subset seed, plan text truncated to a fixed cap.
  * robust: each LLM call retries with exponential backoff; a call that never yields a
    parseable label leaves the row's label empty and is reported (never silently TP/FP).
  * the raters are blind to each other — Rater B's prompt never contains Rater A's label.

Usage:
    python adjudicate.py               # label the corpus in place (atomic write-back)
    python adjudicate.py --limit N     # smoke: only label the first N rows (rater A)
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import tempfile
import threading
import time
from collections.abc import Callable
from functools import lru_cache
from typing import Any

import anthropic

from rebar import show_ticket  # public read API — resolves a ticket's current plan

_HERE = os.path.dirname(os.path.abspath(__file__))
RUNS = os.path.abspath(os.path.join(_HERE, "..", "runs"))
CORPUS = os.path.join(RUNS, "adjudication_corpus.jsonl")
RUBRIC_A = os.path.join(_HERE, "adjudicate_rubric_a.md")
RUBRIC_B = os.path.join(_HERE, "adjudicate_rubric_b.md")

# Rater A and Rater B MUST be different models so kappa measures cross-model agreement,
# not one model echoing a shared prompt.
MODEL_A = "claude-opus-4-8"
MODEL_B = "claude-sonnet-5"

SUBSET_SEED = 4099
# The AC needs a >= 50-finding double-labeled subset with >= 50 USABLE binary pairs after
# excluding ambiguous. With ~20-30% ambiguous, target 110 so >= 50 survive exclusion.
SUBSET_TARGET = 110
PLAN_CAP = 6000  # chars of plan text handed to the rater (bounds tokens)
# These models (opus-4-8 / sonnet-5) use extended thinking by DEFAULT. A small cap starves
# them: the whole budget is spent thinking (stop_reason=max_tokens) and NO text block is
# emitted, so the row silently fails to parse. Give ample room for reasoning + the short JSON
# answer. Observed: ~650 thinking tokens + ~200 answer on a hard finding; 2000 is safe.
MAX_TOKENS = 2000
# This env's rate limiter refuses sustained concurrency, so the pass is fully serialized;
# the value of concurrency here would be zero and its failure mode (429 storms) is real.
MAX_RETRIES = 10
THROTTLE_S = 0.8  # min seconds between call starts (respect the limiter proactively)
# Persist the whole corpus (atomic tmp+os.replace) every CHECKPOINT_EVERY labels so a
# crash / rate-limit exhaustion loses at most this many rows, not the entire pass, and the
# next run resumes from the persisted labels (via _needs). The corpus is ~400 rows, so a
# full rewrite per checkpoint costs far less than one LLM call; a genuinely large corpus
# would instead want an append-only journal or a keyed store (O(1) per label, not O(n)).
CHECKPOINT_EVERY = 5

_client = anthropic.Anthropic()
_throttle_lock = threading.Lock()
_last_call = [0.0]


def _throttle() -> None:
    """Space call starts >= THROTTLE_S apart so we stay under the rate limit instead of
    hammering it into 429s (which this environment does not recover from under load)."""
    with _throttle_lock:
        wait = THROTTLE_S - (time.monotonic() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.monotonic()


@lru_cache(maxsize=None)
def _plan_text(ticket_id: str) -> str:
    try:
        t = show_ticket(ticket_id)
    except Exception:  # noqa: BLE001 — a missing plan is context we can note, not fatal
        return "(ticket plan unavailable)"
    desc = (t or {}).get("description") or ""
    return desc[:PLAN_CAP]


def _finding_block(row: dict[str, Any]) -> str:
    return (
        f"finding_id: {row['finding_id']}\n"
        f"source: {row['source']}\n"
        f"criterion: {row['criterion']}   (all criteria: {', '.join(row['criteria']) or 'none'})\n"
        f"decision: {row['decision']}   drop_reason: {row.get('drop_reason')}\n"
        f"severity: {row.get('severity')}\n"
        f"location: {row.get('location')}\n"
        f"finding: {row.get('finding')}\n"
        f"suggested_fix: {row.get('suggested_fix')}\n"
        f"\n--- ticket plan ---\n{_plan_text(row['ticket_id'])}\n"
    )


def _call(model: str, system: str, user: str) -> str:
    delay = 1.0
    last_err: Exception | None = None
    for _ in range(MAX_RETRIES):
        try:
            _throttle()
            resp = _client.messages.create(
                model=model,
                max_tokens=MAX_TOKENS,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            return "".join(
                b.text for b in resp.content if getattr(b, "type", None) == "text"
            )
        except (
            anthropic.RateLimitError,
            anthropic.APITimeoutError,
            anthropic.APIConnectionError,
            anthropic.InternalServerError,
        ) as e:  # transient — back off and retry
            last_err = e
            time.sleep(delay)
            delay = min(delay * 2, 30.0)
        except anthropic.APIStatusError as e:
            # 429 (incl. proxy-wrapped rate limits) + 5xx are transient — retry; other
            # 4xx (bad request, auth) are permanent — fail fast.
            if e.status_code != 429 and e.status_code < 500:
                raise
            last_err = e
            time.sleep(delay)
            delay = min(delay * 2, 30.0)
    raise RuntimeError(f"LLM call failed after {MAX_RETRIES} retries: {last_err}")


_LABEL_TOKEN_RE = re.compile(r'"?tp_fp"?\s*[:=]\s*"?(TP|FP|ambiguous)"?', re.IGNORECASE)


def _parse_label(text: str) -> tuple[str, str]:
    """Extract (tp_fp, rationale) from the model's reply; ('', '') on failure.

    Robust to a model that REASONS before emitting the JSON object (a greedy ``{.*}``
    grabs a stray brace in the prose and fails json.loads). We scan flat ``{...}``
    candidates from the END (the answer object is last, flat, no nested braces) and fall
    back to a bare ``tp_fp: <label>`` token if no JSON object parses.
    """
    for cand in reversed(re.findall(r"\{[^{}]*\}", text, re.DOTALL)):
        try:
            obj = json.loads(cand)
        except json.JSONDecodeError:
            continue
        label = str(obj.get("tp_fp", "")).strip()
        if label in {"TP", "FP", "ambiguous"}:
            return label, str(obj.get("rationale", "")).strip()
    m = _LABEL_TOKEN_RE.search(text)
    if m:
        return m.group(1).upper() if m.group(1).lower() != "ambiguous" else "ambiguous", ""
    return "", ""


def _adjudicate_one(model: str, rubric: str, row: dict[str, Any]) -> tuple[str, str]:
    return _parse_label(_call(model, rubric, _finding_block(row)))


def _key(row: dict[str, Any]) -> tuple[str, str]:
    """Stable per-row key. finding_ids can collide across tickets, so key on the pair."""
    return (row["ticket_id"], str(row["finding_id"]))


def _stratified_subset(
    rows: list[dict[str, Any]], target: int, seed: int
) -> set[tuple[str, str]]:
    """(ticket_id, finding_id) keys of a stratified-by-criterion subset of >= min(target, len)."""
    rng = random.Random(seed)
    by_crit: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_crit.setdefault(r["criterion"], []).append(r)
    for bucket in by_crit.values():
        rng.shuffle(bucket)
    picked: list[dict[str, Any]] = [by_crit[c].pop() for c in sorted(by_crit)]
    rest = [r for b in by_crit.values() for r in b]
    rng.shuffle(rest)
    picked.extend(rest[: max(0, target - len(picked))])
    return {_key(r) for r in picked}


def _needs(row: dict[str, Any], field: str) -> bool:
    return row.get(field) not in {"TP", "FP", "ambiguous"}


def _atomic_write(rows: list[dict[str, Any]], path: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _run_phase(
    todo: list[dict[str, Any]],
    label_row: Callable[[dict[str, Any]], None],
    all_rows: list[dict[str, Any]],
) -> None:
    """Label each row in ``todo`` in order, CHECKPOINTING the full corpus atomically every
    ``CHECKPOINT_EVERY`` labels (and once at the end).

    This is the crash-safety spine: a single terminal write would lose every billable label
    on any mid-pass death (crash, rate-limit exhaustion, machine sleep). Persisting
    incrementally means an interruption forfeits at most ``CHECKPOINT_EVERY`` rows, and the
    next run resumes from disk (``_needs`` skips already-labeled rows) instead of re-billing
    the whole corpus. Rows in ``todo`` are the same objects held in ``all_rows``, so writing
    ``all_rows`` after each mutation persists a complete, consistent corpus every time.
    """
    for i, row in enumerate(todo, 1):
        label_row(row)
        if i % CHECKPOINT_EVERY == 0:
            _atomic_write(all_rows, CORPUS)
    _atomic_write(all_rows, CORPUS)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=0, help="smoke: label first N rows only")
    args = ap.parse_args(argv)

    with open(CORPUS) as fh:
        rows = [json.loads(line) for line in fh]
    work = rows[: args.limit] if args.limit else rows

    # RESUMABLE: only call the LLM for rows still lacking a valid label, so repeated runs
    # converge coverage cheaply (a rate-limited row is retried next run, not re-billed).

    # ── Rater A: every row (fill missing) ─────────────────────────────────────
    def _a(row: dict[str, Any]) -> None:
        try:
            label, rationale = _adjudicate_one(MODEL_A, _read(RUBRIC_A), row)
        except Exception as e:  # noqa: BLE001 — one bad row must not abort the sweep
            row["tp_fp"] = row["rater_a"] = ""
            row["rationale"] = f"(adjudication error: {e})"
            return
        row["tp_fp"] = label
        row["rater_a"] = label
        row["rationale"] = rationale

    a_todo = [r for r in work if _needs(r, "rater_a")]
    _run_phase(a_todo, _a, rows)
    a_done = sum(1 for r in work if not _needs(r, "rater_a"))
    print(f"[rater A / {MODEL_A}] labeled {a_done}/{len(work)} rows ({len(a_todo)} attempted)")

    # ── Rater B: stratified subset, blind (fill missing) ──────────────────────
    if not args.limit:
        subset = _stratified_subset(rows, SUBSET_TARGET, SUBSET_SEED)
        b_rows = [r for r in rows if _key(r) in subset]

        def _b(row: dict[str, Any]) -> None:
            try:
                label, _ = _adjudicate_one(MODEL_B, _read(RUBRIC_B), row)
            except Exception:  # noqa: BLE001 — leave unlabeled, reported below
                label = ""
            row["rater_b"] = label

        b_todo = [r for r in b_rows if _needs(r, "rater_b")]
        _run_phase(b_todo, _b, rows)
        b_done = sum(1 for r in b_rows if not _needs(r, "rater_b"))
        print(
            f"[rater B / {MODEL_B}] labeled {b_done}/{len(b_rows)} subset rows "
            f"({len(b_todo)} attempted)"
        )

    _atomic_write(rows, CORPUS)  # final flush (also covers the --limit path)
    print(f"wrote labels -> {CORPUS}")
    return 0


@lru_cache(maxsize=None)
def _read(path: str) -> str:
    with open(path) as fh:
        return fh.read()


if __name__ == "__main__":
    raise SystemExit(main())
