"""Engine comparison: the pipeline vs the agentic plan-review engine on the same plans (ADR 0123).

An opt-in, OPERATOR-RUN harness — never a CI job. For each whole-plan row of the frozen
plan-review replay labels file (``labels-*.jsonl`` under the configured replay dir, built by
:mod:`rebar.llm.evals.plan_replay`), it seeds a disposable git + rebar store
(:func:`rebar.llm.evals.eval_solver.case_store`) with the row's ``file_impact`` files at the
harness checkout's HEAD, creates one ticket carrying the row's plan, and reviews it with
``engine="pipeline"`` and ``engine="agentic"`` — unsigned, no sidecar, ``source="local"``, with a
config and runner rooted at the store so every tool in both engines reads only the store.

It reports verdict agreement, the 2x2 confusion matrix, blocking flips, finding overlap and each
engine's agreement with the row's historical pipeline verdict. The criterion eval specs are NOT
this corpus: their cases are single-criterion fragments the Layer-1 floor blocks before either
engine runs, and their gold labels describe one criterion rather than the whole plan.

Run (live, billable): ``python -m rebar.llm.evals.engine_comparison --out <path>``.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rebar.types import FileImpactEntry

#: Minimum token-set Jaccard similarity for two findings to count as the same defect.
MATCH_THRESHOLD = 0.5
ENGINES = ("pipeline", "agentic")
DECIDED = ("PASS", "BLOCK")
#: Resolution classes worth one retry (a transient provider condition).
RETRYABLE = frozenset({"WAIT_AND_RETRY", "RETRY_NOW"})
RETRY_DELAY_S = 60
_ERROR_FLAGS = ("llm_unavailable", "input_rejected", "config_fault")

__all__ = [
    "DECIDED",
    "ENGINES",
    "MATCH_THRESHOLD",
    "classify_outcome",
    "corpus_digest",
    "load_rows",
    "main",
    "match_findings",
    "select_rows",
    "summarize",
]


# ── corpus ───────────────────────────────────────────────────────────────────────
def load_rows(path: str | Path) -> list[dict[str, Any]]:
    """The verified rows of a replay labels JSONL, oldest review first."""
    rows = [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
    rows = [r for r in rows if r.get("verified")]
    return sorted(rows, key=lambda r: int(r.get("review_event_ts") or 0))


def select_rows(rows: Sequence[dict[str, Any]], limit: int = 0) -> list[dict[str, Any]]:
    """The first ``limit`` rows (all when 0), always keeping every historical BLOCK row so a
    short run still compares blocking decisions. Order is preserved."""
    if not limit or limit >= len(rows):
        return list(rows)
    blocks = [r for r in rows if r.get("verdict") == "BLOCK"]
    picked = list(blocks)
    for row in rows:
        if len(picked) >= max(limit, len(blocks)):
            break
        if row not in picked:
            picked.append(row)
    return [r for r in rows if r in picked]


def corpus_digest(labels_path: str | Path, rows: Sequence[dict[str, Any]]) -> str:
    """sha256 of the labels file plus the selected review-event ids."""
    h = hashlib.sha256(Path(labels_path).read_bytes())
    for row in rows:
        h.update(str(row.get("review_event_uuid")).encode())
    return h.hexdigest()


# ── outcomes ─────────────────────────────────────────────────────────────────────
def _resolution_class(exc: BaseException | None, coverage: dict[str, Any]) -> str:
    if coverage.get("resolution_class"):
        return str(coverage["resolution_class"])
    from rebar.llm import failure

    outcome = failure.outcome_of(exc) if exc is not None else None
    return (outcome or failure.classify_llm_failure(exc)).resolution_class.value


def classify_outcome(verdict: dict[str, Any] | None, exc: BaseException | None = None) -> dict:
    """One engine's outcome for one row: ``error`` (a raise, or a degraded INDETERMINATE), then
    ``floor`` (decided by the Layer-1 floor), then ``indeterminate``, else the verdict."""
    coverage = (verdict or {}).get("coverage") or {}
    if exc is not None or any(coverage.get(f) for f in _ERROR_FLAGS):
        detail = str(exc) if exc is not None else str(coverage.get("llm_error") or "")
        return {"outcome": "error", "class": _resolution_class(exc, coverage), "detail": detail}
    assert verdict is not None
    if not coverage.get("llm_ran") and verdict.get("blocking"):
        return {"outcome": "floor"}
    if verdict.get("verdict") == "INDETERMINATE":
        return {"outcome": "indeterminate"}
    return {"outcome": verdict.get("verdict")}


def _surfaced(verdict: dict[str, Any] | None) -> list[str]:
    """The non-DET surfaced finding texts of a verdict (DET findings match by construction)."""
    found = [*((verdict or {}).get("blocking") or []), *((verdict or {}).get("advisory") or [])]
    return [str(f.get("finding") or "") for f in found if f.get("tier") != "DET"]


# ── overlap ──────────────────────────────────────────────────────────────────────
def _tokens(text: str) -> set[str]:
    from rebar.llm.plan_review import sidecar

    return set(sidecar._norm_tokens(text).split())


def match_findings(
    pipeline: Sequence[str], agentic: Sequence[str], threshold: float = MATCH_THRESHOLD
) -> dict[str, int]:
    """Greedy one-to-one matching, highest Jaccard first (ties: pipeline then agentic order)."""
    p_tok = [_tokens(t) for t in pipeline]
    a_tok = [_tokens(t) for t in agentic]
    pairs = []
    for i, p in enumerate(p_tok):
        for j, a in enumerate(a_tok):
            union = p | a
            score = len(p & a) / len(union) if union else 0.0
            if score >= threshold:
                pairs.append((-score, i, j))
    used_p: set[int] = set()
    used_a: set[int] = set()
    for _, i, j in sorted(pairs):
        if i not in used_p and j not in used_a:
            used_p.add(i)
            used_a.add(j)
    matched = len(used_p)
    return {
        "matched": matched,
        "pipeline_only": len(pipeline) - matched,
        "agentic_only": len(agentic) - matched,
    }


# ── summary ──────────────────────────────────────────────────────────────────────
def summarize(items: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Agreement, confusion matrix, flips, overlap, excluded tallies and historical agreement."""
    both = [
        i
        for i in items
        if i["pipeline"]["outcome"] in DECIDED and i["agentic"]["outcome"] in DECIDED
    ]
    matrix = {p: {a: 0 for a in DECIDED} for p in DECIDED}
    overlap = {"matched": 0, "pipeline_only": 0, "agentic_only": 0}
    for item in both:
        matrix[item["pipeline"]["outcome"]][item["agentic"]["outcome"]] += 1
        for key, value in item["overlap"].items():
            overlap[key] += value
    agree = sum(matrix[v][v] for v in DECIDED)
    excluded: dict[str, dict[str, int]] = {e: {} for e in ENGINES}
    historical: dict[str, dict[str, int]] = {}
    for engine in ENGINES:
        hits = total = 0
        for item in items:
            outcome = item[engine]["outcome"]
            if outcome not in DECIDED:
                excluded[engine][outcome] = excluded[engine].get(outcome, 0) + 1
            elif item["historical_pipeline_verdict"] in DECIDED:
                total += 1
                hits += outcome == item["historical_pipeline_verdict"]
        historical[engine] = {"agree": hits, "n": total}
    errors = {e: excluded[e].get("error", 0) for e in ENGINES}
    return {
        "n_items": len(items),
        "n_compared": len(both),
        "agreement": (agree / len(both)) if both else None,
        "confusion_matrix": matrix,
        "blocking_flips": matrix["PASS"]["BLOCK"] + matrix["BLOCK"]["PASS"],
        "overlap": overlap,
        "excluded": excluded,
        "historical_pipeline_agreement": historical,
        # A run that compared nothing, or where an engine errored on every row, proves nothing.
        "valid": bool(both) and all(errors[e] < len(items) for e in ENGINES),
    }


# ── the live run ─────────────────────────────────────────────────────────────────
def _fixture_files(row: dict[str, Any], checkout: Path) -> tuple[dict[str, str], int]:
    """Each ``file_impact`` path's content at the checkout's HEAD; missing paths are counted."""
    files: dict[str, str] = {}
    missing = 0
    for entry in row.get("file_impact") or []:
        path = entry.get("path") if isinstance(entry, dict) else str(entry)
        if not path:
            continue
        # raw-git-ok: reads the harness checkout's own committed tree, not the tracker
        shown = subprocess.run(
            ["git", "-C", str(checkout), "show", f"HEAD:{path}"],
            capture_output=True,
            text=True,
            check=False,  # a path absent at HEAD is counted, not fatal
        )
        if shown.returncode == 0:
            files[path] = shown.stdout
        else:
            missing += 1
    return files, missing


def _review_once(tid: str, engine: str, store: str, cfg: Any, runner: Any) -> dict:
    import rebar.llm

    try:
        verdict = rebar.llm.review_plan(
            tid,
            engine=engine,
            sign=False,
            emit_sidecar=False,
            repo_root=store,
            source="local",
            config=cfg,
            runner=runner,
        )
    except Exception as exc:  # noqa: BLE001 — any raise is this engine's recorded error
        return {"verdict": None, **classify_outcome(None, exc)}
    return {"verdict": verdict, **classify_outcome(verdict)}


def _tracker_title(ticket_id: str) -> str | None:
    """The reviewed ticket's title from the harness checkout's own tracker (read-only); the
    labels file records the plan's description but not its title."""
    import warnings

    import rebar

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # a cross-session claim warning is irrelevant here
            return str(rebar.show_ticket(ticket_id).get("title") or "") or None
    except Exception:  # noqa: BLE001 — an unreadable title falls back to the first heading
        return None


def row_title(row: dict[str, Any], lookup: Callable[[str], str | None] | None = None) -> str:
    """The title the disposable ticket carries: the original ticket's title, else the plan's
    first prose line, so the plan and its title never disagree."""
    title = (lookup or _tracker_title)(str(row.get("ticket_id") or ""))
    if title:
        return title
    for line in str(row.get("description") or "").splitlines():
        text = line.strip()
        if text and not text.startswith("#"):
            return text[:120]
    return f"plan {row.get('ticket_id')}"


def review_row(
    row: dict[str, Any],
    *,
    checkout: Path,
    make_runner: Callable[[Any], Any],
    sleep: Callable[[float], None] = time.sleep,
    title_lookup: Callable[[str], str | None] | None = None,
) -> dict[str, Any]:
    """Review one row under both engines in one disposable store."""
    import rebar
    from rebar.llm.config import resolve_gate_config
    from rebar.llm.evals import eval_solver

    files, missing = _fixture_files(row, checkout)
    with eval_solver.case_store({"files": files}) as store:
        tid = rebar.create_ticket(
            str(row.get("ticket_type") or "task"),
            row_title(row, title_lookup),
            description=str(row.get("description") or ""),
            repo_root=store,
        )
        impact: list[FileImpactEntry] = [
            {"path": str(e["path"]), "reason": str(e.get("reason") or "")}
            for e in row.get("file_impact") or []
            if isinstance(e, dict) and e.get("path")
        ]
        if impact:
            rebar.set_file_impact(tid, impact, repo_root=store)
        cfg = replace(resolve_gate_config(store), repo_path=store, tickets_path=None)
        runner = eval_solver._rerooted(make_runner(cfg), store)
        record: dict[str, Any] = {
            "review_event_uuid": row.get("review_event_uuid"),
            "ticket_id": row.get("ticket_id"),
            "historical_pipeline_verdict": row.get("verdict"),
            "missing_fixture_files": missing,
        }
        for engine in ENGINES:
            result = _review_once(tid, engine, store, cfg, runner)
            if result["outcome"] == "error" and result.get("class") in RETRYABLE:
                sleep(RETRY_DELAY_S)
                result = _review_once(tid, engine, store, cfg, runner)
            record[engine] = {k: v for k, v in result.items() if k != "verdict"}
            record[engine]["findings"] = _surfaced(result["verdict"])
        record["overlap"] = match_findings(
            record["pipeline"]["findings"], record["agentic"]["findings"]
        )
    return record


def run(
    rows: Sequence[dict[str, Any]],
    *,
    labels_path: Path,
    out: Path | None,
    checkout: Path,
    make_runner: Callable[[Any], Any],
    resume: dict[str, Any] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    title_lookup: Callable[[str], str | None] | None = None,
) -> dict[str, Any]:
    """Review every row, rewriting ``out`` after each item; ``resume`` skips clean records."""
    done = {
        i["review_event_uuid"]: i
        for i in (resume or {}).get("items", [])
        if all(i[e]["outcome"] != "error" for e in ENGINES)
    }
    items: list[dict[str, Any]] = []
    results: dict[str, Any] = {}
    for row in rows:
        uuid = row.get("review_event_uuid")
        item = done.get(uuid) or review_row(
            row, checkout=checkout, make_runner=make_runner, sleep=sleep, title_lookup=title_lookup
        )
        items.append(item)
        results = _results(labels_path, rows, items, dry_run=False)
        if out is not None:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(results, indent=2, sort_keys=True) + "\n")
    return results or _results(labels_path, rows, items, dry_run=False)


def _results(labels_path, rows, items, *, dry_run: bool) -> dict[str, Any]:
    summary = summarize(items)
    return {
        "schema_version": 1,
        # The file NAME (content-hashed), never a local path: the results are committed.
        "corpus": Path(labels_path).name,
        "corpus_digest": corpus_digest(labels_path, rows),
        "match_threshold": MATCH_THRESHOLD,
        "recorded_at": datetime.now(UTC).isoformat(),
        "dry_run": dry_run,
        "valid": summary["valid"],
        "summary": summary,
        "items": list(items),
    }


def _default_labels() -> Path:
    from rebar.llm.config_readers import replay_dir

    # The frozen whole-plan replay labels (11 rows at the time of writing), under the corpus
    # root config resolves — never a hard-coded path.
    found = sorted(replay_dir().glob("labels-*.jsonl"))
    if not found:
        raise SystemExit(f"no labels-*.jsonl under {replay_dir()}")
    return found[0]


def main(argv: list[str] | None = None, *, make_runner: Callable[[Any], Any] | None = None) -> int:
    """Operator CLI. ``--dry-run`` prints the corpus and digest and calls no model."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(prog="python -m rebar.llm.evals.engine_comparison")
    parser.add_argument("--labels", type=Path, default=None)
    parser.add_argument("--limit", type=int, default=0, help="cap the corpus (keeps BLOCK rows)")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--resume", action="store_true", help="skip rows already in --out")
    parser.add_argument("--dry-run", action="store_true", help="print the corpus, call no model")
    args = parser.parse_args(argv)

    labels = args.labels or _default_labels()
    rows = select_rows(load_rows(labels), args.limit)
    if args.dry_run:
        listing = {
            "corpus_digest": corpus_digest(labels, rows),
            "n_items": len(rows),
            "items": [{"ticket_id": r.get("ticket_id"), "verdict": r.get("verdict")} for r in rows],
        }
        sys.stdout.write(json.dumps(listing, indent=2) + "\n")
        return 0
    resume = None
    if args.resume and args.out and args.out.exists():
        resume = json.loads(args.out.read_text())
    if make_runner is None:
        from rebar.llm.runner import get_runner

        make_runner = get_runner
    checkout = Path(
        subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True
        ).stdout.strip()
    )
    results = run(
        rows,
        labels_path=labels,
        out=args.out,
        checkout=checkout,
        make_runner=make_runner,
        resume=resume,
    )
    sys.stdout.write(json.dumps(results["summary"], indent=2, sort_keys=True) + "\n")
    return 0 if results["valid"] else 1


if __name__ == "__main__":  # pragma: no cover — exercised by the module CLI
    raise SystemExit(main())
