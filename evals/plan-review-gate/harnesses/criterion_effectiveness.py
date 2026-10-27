#!/usr/bin/env python3
"""Standing per-criterion effectiveness recorder (epic 6982, extends R7).

R7's ``gate_eval_instrumentation.py`` derives its per-criterion metrics by JOINING each reviewed
ticket to the **outcome corpus** (post-claim edit class / reopen / force-close), which
``mine_outcome_corpus.py`` mines from the ``tickets`` orphan branch's git objects — a periodic
batch job whose FP signal is the post-hoc force-close/reopen fact.

This recorder is DISTINCT and complementary: its signal source is the sidecar **re-review history
ALONE** (no outcome corpus, no git-object walk), so it is a cheap STANDING recorder that
accumulates per-criterion firings at ZERO marginal LLM cost and computes a *detection* proxy R7
does not, plus a *within-review-convergence* blocking-FP proxy distinct from R7's force-close one.

WHAT IT DOES
    --record [--backfill]   Read persisted REVIEW_RESULT sidecars (mirroring the file enumeration
                            + v1/v2 schema guard of rebar.llm.plan_review.sidecar.all_review_results)
                            and APPEND one lean firing row per (review-round, finding) into the
                            append-only, prune-immune ledger runs/criterion_firings.jsonl. Idempotent
                            (dedup key = ticket + review_ts + round_uuid + norm_id); AUTO-INCLUDES any
                            criterion id it sees. --backfill scans the whole corpus; default scans
                            only reviews past the ledger's watermark (max recorded review_ts).
    --report [--window N]   Compute per-criterion effectiveness over the trailing window (the N
                            most-recently-reviewed tickets) from the ledger ALONE, and write
                            runs/criterion_effectiveness.json — AUTO-INCLUDING every criterion id in
                            the ledger, so R1/R3/R4's newly-shipped advisory criteria are monitored
                            with no per-criterion wiring.

    python criterion_effectiveness.py --record --backfill        # seed the ledger from all sidecars
    python criterion_effectiveness.py --report --no-refresh       # metrics over the committed ledger

WHY A STANDING LEDGER (not just reading sidecars at report time): the sidecar's own retention prune
caps history at RETAIN_PER_TICKET=50 rounds/ticket, so old firings are eventually dropped on disk.
The append-only ledger captures each firing before that prune — that durability is the recorder's
reason to exist. Firing row schema (short keys keep the committed JSONL compact + jq-friendly):
    t  ticket_id            n  norm_id (finding fingerprint, criteria-scoped)
    ts review_ts_ns (int)   u  fix_unit_key (CRITERIA-FREE fingerprint; cross-round match key)
    r  round_uuid           d  decision (block|advisory|dropped|indeterminate|overflow)
    v  verdict (PASS/BLOCK) s  severity           p  priority (float|null)
    c  criteria (list[str]) x  drop_reason (Pass-3 floor: novelty|completion|null)

The pure ``firings_from_review`` / ``compute_effectiveness`` are CI-tested in
tests/unit/test_criterion_effectiveness.py. Only --record touches the live store (importing the
production sidecar fingerprint helpers); --report/compute read the committed ledger with no rebar
import, exactly as R7's committed corpus is the CI-visible artifact.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parent
RUNS = HERE.parent / "runs"
FIRINGS = RUNS / "criterion_firings.jsonl"
METRICS_OUT = RUNS / "criterion_effectiveness.json"

EVENT_SUFFIX = "-REVIEW_RESULT.json"
# The sidecar schemas all_review_results accepts (kept in lockstep with sidecar.py's guard).
USABLE_SCHEMAS = ("plan_review_result_v1", "plan_review_result_v2")
# The de-escalation decision: a finding the gate found but did NOT surface as block/advisory. It
# SUBSUMES both a Pass-3 threshold drop (drop_reason null) and the novelty/completion convergence
# floors (which stamp drop_reason) — the blocking-FP proxy keys off decision=="dropped", so it has
# signal even though those floors are inert-by-default in the production corpus (all observed
# `dropped` findings carry drop_reason null). FLOOR_DROP_REASONS is retained only to LABEL, in the
# per-firing `x` field, which drops were floor-driven for future floor-era segmentation.
DROPPED_DECISION = "dropped"
FLOOR_DROP_REASONS = ("novelty", "completion")
DEFAULT_WINDOW = 400


# ── pure: firing extraction (unit-tested with injected fingerprint fns) ───────────────────────


def parse_sidecar_name(fname: str) -> tuple[int, str] | None:
    """``<ts_ns>-<uuid>-REVIEW_RESULT.json`` → ``(ts_ns, uuid)``; None if it does not match.
    Timestamp-prefixed filenames are the same identity ``all_review_results`` orders by."""
    if not fname.endswith(EVENT_SUFFIX) or fname.startswith("."):
        return None
    stem = fname[: -len(EVENT_SUFFIX)]
    ts_str, sep, uuid = stem.partition("-")
    if not sep or not ts_str.isdigit() or not uuid:
        return None
    return int(ts_str), uuid


def firings_from_review(
    ticket_id: str,
    ts_ns: int,
    round_uuid: str,
    payload: dict[str, Any],
    fix_unit_key: Callable[[dict[str, Any]], str],
    norm_id: Callable[[dict[str, Any]], str],
) -> list[dict[str, Any]]:
    """One firing row per finding in a single review sidecar. ``fix_unit_key``/``norm_id`` are
    injected (the production ``sidecar`` helpers at record time; trivial fns in tests) so this
    function is pure and rebar-free. Findings with no criteria are still recorded (criteria ``[]``)
    so the total-firing denominator is honest, but they contribute to no per-criterion metric."""
    verdict = payload.get("verdict")
    rows: list[dict[str, Any]] = []
    for f in payload.get("findings") or []:
        if f.get("decision") == "indeterminate":
            continue  # an abstain is not a criterion firing (and never enters a metric)
        rows.append(
            {
                "t": ticket_id,
                "ts": ts_ns,
                "r": round_uuid,
                "v": verdict,
                "c": list(f.get("criteria") or []),
                "n": f.get("norm_id") or norm_id(f),
                "u": fix_unit_key(f),
                "d": f.get("decision"),
                "s": f.get("severity"),
                "p": f.get("priority"),
                "x": f.get("drop_reason"),
            }
        )
    return rows


def _dedup_key(row: dict[str, Any]) -> tuple[str, int, str, str]:
    return (row["t"], int(row["ts"]), row["r"], row["n"])


# ── pure: per-criterion effectiveness over the ledger (unit-tested on fixtures) ───────────────


def _window_tickets(rows: list[dict[str, Any]], window: int | None) -> set[str]:
    """The trailing window = the ``window`` most-recently-reviewed tickets (by max review_ts).
    Windowing per TICKET (not per row) keeps each ticket's full round history intact, which the
    remediation/suppression disposition below requires. ``window`` None/<=0 keeps every ticket."""
    last_ts: dict[str, int] = {}
    for r in rows:
        t, ts = r["t"], int(r["ts"])
        if ts > last_ts.get(t, -1):
            last_ts[t] = ts
    if not window or window <= 0:
        return set(last_ts)
    ranked = sorted(last_ts, key=lambda t: last_ts[t], reverse=True)
    return set(ranked[:window])


def compute_effectiveness(
    rows: list[dict[str, Any]], window: int | None = DEFAULT_WINDOW
) -> dict[str, dict[str, Any]]:
    """Per-criterion trailing effectiveness from the firing ledger ALONE (no outcome corpus).

    For each ticket, rounds are ordered by ``ts``; a blocking fix-unit (keyed by criteria-free
    ``u``) is attributed to every criterion any of its BLOCKING firings cite. Its two fates:

    * REMEDIATED  — absent from the ticket's LATEST round AND that round's verdict is PASS (caught →
      the author fixed it → the ticket passed). Only observable when a round exists after the
      fix-unit's first blocking appearance ("resolvable"). → the ``detection_proxy`` numerator.
    * DE-ESCALATED — the gate itself reversed its block: in a round AFTER its first blocking
      appearance the same fix-unit is found but no longer surfaced as blocking (decision ``dropped``,
      which subsumes a Pass-3 threshold drop and the novelty/completion floors), and it is NOT
      remediated. The gate's own re-review judged the earlier block not block-worthy. → the
      ``blocking_fp_proxy`` numerator.

    Per criterion C:
    * ``detection_proxy``   = REMEDIATED / resolvable-blocking-fix-units   (None if denom 0)
    * ``blocking_fp_proxy`` = DE-ESCALATED / all-blocking-fix-units        (None if denom 0)
    * ``sample_counts``     = every denominator + numerator, so the JSON is self-verifying.
    """
    keep = _window_tickets(rows, window)
    by_ticket: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        if r["t"] in keep:
            by_ticket[r["t"]].append(r)

    blk_units: dict[str, set[tuple[str, str]]] = defaultdict(set)  # C -> {(ticket, fix_unit)}
    resolvable: dict[str, set[tuple[str, str]]] = defaultdict(set)
    remediated: dict[str, set[tuple[str, str]]] = defaultdict(set)
    deescalated: dict[str, set[tuple[str, str]]] = defaultdict(set)
    adv_firings: dict[str, int] = defaultdict(int)
    crit_tickets: dict[str, set[str]] = defaultdict(set)
    crit_firings: dict[str, int] = defaultdict(int)

    for tid, trows in by_ticket.items():
        round_ts = sorted({int(r["ts"]) for r in trows})
        latest_ts = round_ts[-1]
        latest_verdict_pass = any(
            int(r["ts"]) == latest_ts and str(r["v"]).upper() == "PASS" for r in trows
        )
        # Per fix-unit: firings, the criteria that cited it while BLOCKING, first-blocking ts,
        # and its rows in the latest round.
        units: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"blk_crits": set(), "first_blk": None, "drop_ts": [], "in_latest": False}
        )
        for r in trows:
            for c in r["c"]:
                crit_firings[c] += 1
                crit_tickets[c].add(tid)
                if r["d"] == "advisory":
                    adv_firings[c] += 1
            u = r["u"]
            info = units[u]
            ts = int(r["ts"])
            if r["d"] == "block":
                info["blk_crits"].update(r["c"])
                if info["first_blk"] is None or ts < info["first_blk"]:
                    info["first_blk"] = ts
            if r["d"] == DROPPED_DECISION:
                info["drop_ts"].append(ts)
            if ts == latest_ts:
                info["in_latest"] = True

        for u, info in units.items():
            if not info["blk_crits"]:
                continue  # never a blocking fix-unit → not a blocking metric
            first_blk = info["first_blk"] or latest_ts
            is_resolvable = latest_ts > first_blk
            is_remediated = is_resolvable and (not info["in_latest"]) and latest_verdict_pass
            # The gate reversed its own block: the same fix-unit is found but DROPPED in a later
            # round, and it was never remediated to a PASS → the conservative within-review FP proxy.
            de_escalated = any(dts > first_blk for dts in info["drop_ts"])
            is_suppressed = de_escalated and not is_remediated
            for c in info["blk_crits"]:
                key = (tid, u)
                blk_units[c].add(key)
                if is_resolvable:
                    resolvable[c].add(key)
                if is_remediated:
                    remediated[c].add(key)
                if is_suppressed:
                    deescalated[c].add(key)

    out: dict[str, dict[str, Any]] = {}
    for c in sorted(set(crit_firings) | set(blk_units)):
        n_blk = len(blk_units[c])
        n_res = len(resolvable[c])
        n_rem = len(remediated[c])
        n_deesc = len(deescalated[c])
        out[c] = {
            "detection_proxy": (n_rem / n_res) if n_res else None,
            "blocking_fp_proxy": (n_deesc / n_blk) if n_blk else None,
            "sample_counts": {
                "blocking_fix_units": n_blk,
                "resolvable_fix_units": n_res,
                "remediated": n_rem,
                "deescalated": n_deesc,
                "advisory_firings": adv_firings[c],
                "tickets": len(crit_tickets[c]),
                "firings": crit_firings[c],
            },
        }
    return out


# ── I/O layer (only --record touches the live store) ──────────────────────────────────────────


def _tracker_dir() -> Path:
    try:
        from rebar import config

        return Path(config.tracker_dir(None))
    except Exception:  # noqa: BLE001 — offline fallback to the conventional on-disk store
        return HERE.parents[3] / ".tickets-tracker"


def _fingerprint_fns() -> tuple[Callable[[dict[str, Any]], str], Callable[[dict[str, Any]], str]]:
    """The PRODUCTION fingerprint helpers — this recorder never re-implements fingerprint logic."""
    from rebar.llm.plan_review import sidecar

    return sidecar.fix_unit_key, (lambda f: f.get("norm_id") or sidecar.norm_id(f))


def load_firings(path: Path = FIRINGS) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def _write_firings(rows: list[dict[str, Any]], path: Path = FIRINGS) -> None:
    """Deterministic atomic write (sorted by ts, ticket, norm_id) so the committed ledger is stable
    across machines. Append-only in SEMANTICS: firings are only ever added, never mutated/removed."""
    rows = sorted(rows, key=lambda r: (int(r["ts"]), r["t"], r["n"], r["d"] or ""))
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))
    os.replace(tmp, path)


def record(backfill: bool = False, tracker: Path | None = None) -> int:
    """Scan the live sidecar corpus and append new firings to the ledger. Returns rows added.
    Idempotent: a candidate whose dedup key already exists is skipped. Default (incremental) mode
    only reads reviews past the ledger's watermark; --backfill reads the whole corpus."""
    tracker = tracker or _tracker_dir()
    fu_fn, nid_fn = _fingerprint_fns()
    existing = load_firings()
    seen = {_dedup_key(r) for r in existing}
    watermark = max((int(r["ts"]) for r in existing), default=-1)
    added: list[dict[str, Any]] = []
    if not tracker.exists():
        raise SystemExit(f"tracker dir not found: {tracker}")
    for ticket_dir in sorted(p for p in tracker.iterdir() if p.is_dir()):
        for fname in sorted(os.listdir(ticket_dir)):
            parsed = parse_sidecar_name(fname)
            if parsed is None:
                continue
            ts_ns, uuid = parsed
            if not backfill and ts_ns <= watermark:
                continue
            try:
                event = json.loads((ticket_dir / fname).read_text())
            except (OSError, ValueError):
                continue
            payload = event.get("data") if isinstance(event, dict) else None
            if not (isinstance(payload, dict) and payload.get("schema") in USABLE_SCHEMAS):
                continue
            for row in firings_from_review(ticket_dir.name, ts_ns, uuid, payload, fu_fn, nid_fn):
                key = _dedup_key(row)
                if key not in seen:
                    seen.add(key)
                    added.append(row)
    if added:
        _write_firings(existing + added)
    return len(added)


def report(window: int = DEFAULT_WINDOW) -> dict[str, dict[str, Any]]:
    rows = load_firings()
    if not rows:
        raise SystemExit(f"firing ledger empty/absent: {FIRINGS} (run --record --backfill first)")
    metrics = compute_effectiveness(rows, window=window)
    METRICS_OUT.write_text(json.dumps(metrics, indent=2, sort_keys=True) + "\n")
    return metrics


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--record", action="store_true", help="append firings from the live sidecar corpus")
    ap.add_argument("--backfill", action="store_true", help="with --record: scan the WHOLE corpus")
    ap.add_argument("--report", action="store_true", help="write per-criterion effectiveness JSON")
    ap.add_argument("--window", type=int, default=DEFAULT_WINDOW, help="trailing window (# tickets)")
    ap.add_argument(
        "--no-refresh", action="store_true", help="(report) read the committed ledger; do not record"
    )
    args = ap.parse_args(argv)
    if not (args.record or args.report):
        ap.error("pass --record and/or --report")

    if args.record:
        n = record(backfill=args.backfill)
        print(f"recorded {n} new firing(s) -> {FIRINGS} ({len(load_firings())} total)")

    if args.report:
        metrics = report(window=args.window)
        nonnull_det = sum(1 for m in metrics.values() if m["detection_proxy"] is not None)
        nonnull_fp = sum(1 for m in metrics.values() if m["blocking_fp_proxy"] is not None)
        print(
            f"wrote {len(metrics)} criteria -> {METRICS_OUT} "
            f"({nonnull_det} with detection_proxy, {nonnull_fp} with blocking_fp_proxy)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
