#!/usr/bin/env python3
"""E6 judge-reliability driver (ticket a880 — epic 6982 plan-review calibration).

Two bounded judge-reliability experiments over the EXISTING plan-review gate (no ``src/rebar``
behavior changes — E6 is measurement):

* **Exp A — self-consistency** of the Pass-2 verify / Pass-3 decide judge over the R5 cohort
  (``adjudication_corpus.jsonl`` rows with ``criterion ∈ {G6,E4,T3}``). Re-judges N=50 frozen
  findings 3× each and measures agreement (Fleiss' κ + raw) on the Pass-3 ``decision``. Gates R5.
* **Exp B — order-shuffle stability** of the gate ``verdict`` over the committed plan-text corpus
  (``corpus_sample.json`` plans with ≥3 top-level ``##`` sections). Re-judges each of N=14 plans
  under 3 distinct section-order permutations and measures agreement on ``verdict``. Gates R3.

The pure agreement/permutation/exclusion math lives in the LLM-free :mod:`e6_metrics` (unit-tested
in ``tests/unit/test_e6_agreement.py``); this driver adds only the I/O + the two live-judge loops.

Subcommands (``python e6_judge_reliability.py <cmd>``):

* ``build-inputs`` — freeze the two committed input files (deterministic; NO LLM). Exp A snapshots
  each sampled finding's ticket plan text; Exp B writes the 3 permutation specs per plan.
* ``run-a`` / ``run-b`` — the billable judge loops (resumable via a raw ``.jsonl`` journal).
* ``analyze`` — compute the agreement tables + ``e6_summary.json`` from the recorded votes (NO LLM).
* ``all`` — build-inputs → run-a → run-b → analyze.

Exp A cost: 50 findings × 3 votes = 150 agentic Pass-2 calls. Exp B cost: 14 plans × 3 perms = 42
full-gate runs. Both are deliberately bounded sub-samples (see the ticket's LLM-spend-honesty note).
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any

# Sibling harness imports (three_pass, gate_lib, harness, exp2_agentic) assume the harnesses dir
# is importable; add it so the driver runs from any cwd.
HARNESS_DIR = Path(__file__).resolve().parent
if str(HARNESS_DIR) not in sys.path:
    sys.path.insert(0, str(HARNESS_DIR))

import e6_metrics as M  # noqa: E402 — after the sys.path shim (pure, LLM-free)

log = logging.getLogger("e6")

# ── Paths ─────────────────────────────────────────────────────────────────────────────
RUNS_DIR = HARNESS_DIR.parent / "runs"
ADJ_CORPUS = RUNS_DIR / "adjudication_corpus.jsonl"
CORPUS_SAMPLE = RUNS_DIR / "corpus_sample.json"

A_INPUTS = RUNS_DIR / "e6_selfconsistency_inputs.jsonl"
A_RAW = RUNS_DIR / "e6_selfconsistency.raw.jsonl"
A_RESULTS = RUNS_DIR / "e6_selfconsistency.jsonl"
A_EXCLUDED = RUNS_DIR / "e6_selfconsistency_excluded.jsonl"
A_AGREEMENT = RUNS_DIR / "e6_selfconsistency_agreement.csv"

B_INPUTS = RUNS_DIR / "e6_ordershuffle_inputs.jsonl"
B_RAW = RUNS_DIR / "e6_ordershuffle.raw.jsonl"
B_RESULTS = RUNS_DIR / "e6_ordershuffle.jsonl"
B_EXCLUDED = RUNS_DIR / "e6_ordershuffle_excluded.jsonl"
B_AGREEMENT = RUNS_DIR / "e6_ordershuffle_agreement.csv"

SUMMARY = RUNS_DIR / "e6_summary.json"

# ── Experiment constants (mirrored in runs/e6_prereg.json) ─────────────────────────────
COHORT = {"G6", "E4", "T3"}
N_FINDINGS = 50
EXP_A_SAMPLE_SEED = 0xA880  # pinned deterministic sampler seed (the ticket short id)
N_PERMS = 3


# ── Small JSONL helpers ────────────────────────────────────────────────────────────────
def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open() as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    with path.open("a") as fh:
        fh.write(json.dumps(row) + "\n")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


# ── build-inputs (deterministic, NO LLM) ───────────────────────────────────────────────
def build_inputs_a() -> None:
    """Freeze Exp A inputs: N=50 findings sampled deterministically from the G6/E4/T3 cohort,
    each with its ticket's plan text snapshotted once from the replay-derived description. A
    ticket whose plan is unretrievable is skipped and the sample is topped up from the cohort
    remainder to hold N=50. Reads the live store READ-ONLY via ``rebar.show_ticket``."""
    import random

    import rebar

    rows = _read_jsonl(ADJ_CORPUS)
    cohort = [r for r in rows if r.get("criterion") in COHORT]
    # dedup by (ticket_id, finding_id) in a deterministic order (all 74 are already unique).
    seen: set[tuple[str, str]] = set()
    deduped: list[dict[str, Any]] = []
    for r in sorted(cohort, key=lambda r: (r["ticket_id"], r["finding_id"])):
        key = (r["ticket_id"], r["finding_id"])
        if key not in seen:
            seen.add(key)
            deduped.append(r)
    log.info(
        "Exp A cohort: %d rows / %d distinct tickets (deduped %d)",
        len(cohort),
        len({r["ticket_id"] for r in cohort}),
        len(deduped),
    )

    order = list(range(len(deduped)))
    random.Random(EXP_A_SAMPLE_SEED).shuffle(order)
    selected: list[dict[str, Any]] = []
    skipped = 0
    for i in order:
        if len(selected) >= N_FINDINGS:
            break
        r = deduped[i]
        try:
            ticket = rebar.show_ticket(r["ticket_id"])
        except Exception:  # noqa: BLE001 — an unretrievable ticket is skipped + topped up
            log.warning("  ticket %s unretrievable — skipping", r["ticket_id"])
            skipped += 1
            continue
        plan = (ticket.get("description") or "").strip()
        title = ticket.get("title") or ""
        if not plan:
            log.warning("  ticket %s has empty plan — skipping", r["ticket_id"])
            skipped += 1
            continue
        selected.append(
            {
                "finding_id": r["finding_id"],
                "ticket_id": r["ticket_id"],
                "title": title,
                "plan": plan,
                "criterion": r["criterion"],
                "finding": r["finding"],
                "location": r.get("location"),
                "suggested_fix": r.get("suggested_fix"),
            }
        )
    if len(selected) < N_FINDINGS:
        raise RuntimeError(
            f"only {len(selected)} findings had retrievable plans (need {N_FINDINGS}); "
            "cohort headroom exhausted"
        )
    selected.sort(key=lambda x: x["finding_id"])
    _write_jsonl(A_INPUTS, selected)
    log.info("wrote %s: %d findings (skipped %d)", A_INPUTS.name, len(selected), skipped)


def build_inputs_b() -> None:
    """Freeze Exp B inputs: for each ``corpus_sample.json`` plan with ≥3 top-level ``##`` sections,
    the 3 distinct section-order permutations (identity + 2 seeded). Deterministic; NO LLM."""
    corpus = json.loads(CORPUS_SAMPLE.read_text())
    permutable = [rec for rec in corpus if M.count_top_sections(rec["plan"]) >= N_PERMS]
    log.info(
        "Exp B: %d/%d plans have >=%d top-level sections", len(permutable), len(corpus), N_PERMS
    )
    out: list[dict[str, Any]] = []
    for rec in permutable:
        perms = M.permute_sections(rec["plan"], rec["id"], n_perms=N_PERMS)
        for perm in perms:
            out.append(
                {
                    "plan_id": rec["id"],
                    "permutation_index": perm["permutation_index"],
                    "section_order": perm["section_order"],
                    "title": rec.get("title", ""),
                    "has_children": rec.get("has_children", False),
                    "plan": perm["text"],
                }
            )
    _write_jsonl(B_INPUTS, out)
    log.info(
        "wrote %s: %d plans x %d permutations = %d rows",
        B_INPUTS.name,
        len(permutable),
        N_PERMS,
        len(out),
    )


# ── run-a: self-consistency judge loop (resumable) ─────────────────────────────────────
def _finding_arg(inp: dict[str, Any]) -> dict[str, Any]:
    """Shape a frozen Exp A input row into the finding dict ``three_pass.pass2_verify`` expects
    (``criteria`` must be ``[criterion]`` so the G6/E4/T3 code-grounded agentic path fires)."""
    return {
        "finding": inp["finding"],
        "criteria": [inp["criterion"]],
        "evidence": [inp.get("location") or ""],
        "impact": inp.get("suggested_fix") or "",
    }


def run_a(repo_root: str) -> None:
    """Exp A: re-judge each frozen finding until 3 substantive votes are collected, applying the
    infra-INDETERMINATE exclusion + retry cap. Journals every raw attempt to ``A_RAW`` so a
    crashed run resumes; materializes the final results/excluded files at the end."""
    import three_pass

    from rebar.llm import gate_source

    inputs = _read_jsonl(A_INPUTS)
    if not inputs:
        raise RuntimeError(f"{A_INPUTS} is empty — run build-inputs first")

    prior = defaultdict(list)
    for att in _read_jsonl(A_RAW):
        prior[att["finding_id"]].append(att)

    handle = gate_source.resolve_gate_handle(None, "local", repo_root)  # gate_source.py:80
    with gate_source.gate_read_root(handle):  # gate_source.py:105 — marks the gate session
        for n, inp in enumerate(inputs, 1):
            fid = inp["finding_id"]
            attempts = list(prior.get(fid, []))
            substantive = [a for a in attempts if not a.get("infra")]
            if len(substantive) >= M.VOTE_TARGET or len(attempts) >= M.ATTEMPT_BUDGET:
                continue  # already complete or budget-exhausted (resume)
            log.info(
                "[A %d/%d] %s (%s) — %d/%d votes so far",
                n,
                len(inputs),
                fid,
                inp["criterion"],
                len(substantive),
                M.VOTE_TARGET,
            )
            farg = _finding_arg(inp)
            while len(substantive) < M.VOTE_TARGET and len(attempts) < M.ATTEMPT_BUDGET:
                v = three_pass.pass2_verify(
                    inp["title"], inp["plan"], farg, repo_root=repo_root, agentic=True
                )
                inner = v.get("verify")
                d = three_pass.pass3_decide(inner)  # deterministic; passes the INNER dict
                infra = M.is_infra_indeterminate_vote(d.get("decision"))
                att = {
                    "finding_id": fid,
                    "ticket_id": inp["ticket_id"],
                    "criterion": inp["criterion"],
                    "attempt": len(attempts),
                    "decision": d.get("decision"),
                    "reason": d.get("reason"),
                    "confidence": d.get("confidence"),
                    "severity": d.get("severity"),
                    "binary": (inner or {}).get("binary"),
                    "mode": v.get("mode"),
                    "tool_calls": v.get("tool_calls"),
                    "infra": infra,
                }
                _append_jsonl(A_RAW, att)
                attempts.append(att)
                if not infra:
                    substantive.append(att)
    _materialize_exp_a()


def _materialize_exp_a() -> None:
    """Rebuild the final ``e6_selfconsistency.jsonl`` (exactly 3 substantive votes/finding) and
    ``e6_selfconsistency_excluded.jsonl`` from the raw journal (source of truth)."""
    by_finding: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for att in _read_jsonl(A_RAW):
        by_finding[att["finding_id"]].append(att)
    results: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for inp in _read_jsonl(A_INPUTS):
        fid = inp["finding_id"]
        pol = M.finalize_votes(by_finding.get(fid, []), is_infra=lambda a: a.get("infra"))
        if pol["excluded"]:
            excluded.append(
                {
                    "finding_id": fid,
                    "criterion": inp["criterion"],
                    **pol,
                    "votes": [a["decision"] for a in pol["votes"]],
                }
            )
            continue
        for i, att in enumerate(pol["votes"], 1):
            results.append(
                {
                    "finding_id": fid,
                    "ticket_id": inp["ticket_id"],
                    "criterion": inp["criterion"],
                    "vote": i,
                    "decision": att["decision"],
                    "reason": att["reason"],
                    "confidence": att["confidence"],
                    "severity": att["severity"],
                    "binary": att["binary"],
                    "mode": att["mode"],
                    "tool_calls": att["tool_calls"],
                }
            )
    _write_jsonl(A_RESULTS, results)
    _write_jsonl(A_EXCLUDED, excluded)
    log.info(
        "Exp A materialized: %d votes across %d findings, %d excluded",
        len(results),
        len(results) // M.VOTE_TARGET if results else 0,
        len(excluded),
    )


# ── run-b: order-shuffle judge loop (resumable) ────────────────────────────────────────
# Untracked store-init markers a plain `git clone` omits (they are git-ignored), but the write
# path needs — chiefly `.env-id` (the composer's "initialized" predicate is `.env-id` present).
_INIT_MARKERS = (".env-id", ".closure-key", ".ensure-applied", ".store-compat.json")


def _real_store_src() -> str:
    """The live tickets store to clone FROM (``REBAR_E6_TRACKER_SRC`` > the resolved,
    symlink-followed tracker dir). The real store is only ever READ/cloned, never written."""
    import rebar.config as rconfig

    return os.environ.get("REBAR_E6_TRACKER_SRC") or os.path.realpath(str(rconfig.tracker_dir()))


def _git_head(store: str) -> str:
    return subprocess.run(
        ["git", "-C", store, "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()


def _clone_store(src: str, dst: Path) -> str:
    """Clone the live tickets store into a THROWAWAY, fully INDEPENDENT dir so Exp B can rewrite a
    ticket's description without touching the real store.

    The real ``.tickets-tracker`` is a git LINKED WORKTREE — its ``.git`` is a FILE pointing back
    at the real repo's object store — so a filesystem copy (``cp -r`` / ``copytree``) would leave a
    write resolving to the REAL tickets branch (``REBAR_SYNC_PUSH=off`` blocks only *push*, not
    local *commit*). ``git clone --local --branch tickets`` instead gives the clone its OWN
    ``.git`` DIRECTORY + object store, so every write stays isolated. This asserts ``.git`` is a
    directory (fail-closed) BEFORE returning, and copies the untracked init markers (``.env-id``
    et al.) that a clone omits so the write path's init check passes."""
    import shutil

    store = dst / "store"
    subprocess.run(
        ["git", "clone", "--local", "--quiet", "--branch", "tickets", src, str(store)], check=True
    )
    gitpath = store / ".git"
    if (
        not gitpath.is_dir()
    ):  # isolation invariant: a directory (independent), never a worktree file
        raise RuntimeError(
            f"clone {store} is not isolated: .git is not a directory (got {gitpath}); refusing to "
            "write against a store that may resolve to the real tickets branch"
        )
    for marker in _INIT_MARKERS:
        srcp = Path(src) / marker
        if srcp.exists():
            shutil.copy2(srcp, store / marker)
    log.info("cloned tickets store %s -> %s (isolated .git dir + init markers)", src, store)
    return str(store)


def _fired_criteria(verdict: dict[str, Any]) -> list[str]:
    """The set of rubric criteria fired (blocking ∪ advisory findings) for the Jaccard metric."""
    crits: set[str] = set()
    for bucket in ("blocking", "advisory"):
        for f in verdict.get(bucket) or []:
            crits.update(f.get("criteria") or [])
    return sorted(crits)


def _is_ticket_not_found(exc: BaseException) -> bool:
    """True iff ``exc`` is a rebar 'ticket not found' failure. A plan sampled into the Exp B
    inputs may since have been archived/removed from the ``tickets`` branch, so it is absent
    from the fresh ``git clone --branch tickets`` clone and ``edit_ticket``/``review_plan``
    raise ``RebarError: … ticket '<id>' not found``. Matched narrowly on the message so the
    guard excludes ONLY the vanished ticket — any OTHER edit/review failure must still
    propagate (the harness never silently pads over a real bug)."""
    return "not found" in str(exc).lower()


def _excluded_attempt(perm: dict[str, Any], attempt: int, reason: str) -> dict[str, Any]:
    """Build a B_RAW journal row marking a plan×permutation EXCLUDED (no verdict recorded). The
    ``excluded`` flag routes the row to ``B_EXCLUDED`` in :func:`_materialize_exp_b` and makes a
    resumed ``run_b`` skip the permutation instead of re-attempting the (permanently) absent
    ticket. ``infra`` is False so it is never confused with a drop-and-re-run infra draw."""
    return {
        "plan_id": perm["plan_id"],
        "permutation_index": perm["permutation_index"],
        "section_order": perm["section_order"],
        "attempt": attempt,
        "verdict": None,
        "fired_criteria": [],
        "coverage_flags": {"llm_unavailable": False, "verify_failed": False},
        "infra": False,
        "excluded": True,
        "exclude_reason": reason,
    }


def run_b(repo_root: str) -> None:
    """Exp B: for each plan permutation, write the permuted plan into a throwaway store clone and
    drive ``review_plan`` (source=local, sign/emit off, force). Re-run any permutation whose
    INDETERMINATE is infra (llm_unavailable / verify_failed). Resumable via ``B_RAW``."""
    import rebar
    from rebar.llm import plan_review

    inputs = _read_jsonl(B_INPUTS)
    if not inputs:
        raise RuntimeError(f"{B_INPUTS} is empty — run build-inputs first")
    by_plan: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in inputs:
        by_plan[row["plan_id"]].append(row)

    prior: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for att in _read_jsonl(B_RAW):
        prior[(att["plan_id"], att["permutation_index"])].append(att)

    src = _real_store_src()
    real_head_before = _git_head(src)  # isolation guard: the real store must not move
    with tempfile.TemporaryDirectory(prefix="e6-store-") as tmp:
        store = _clone_store(src, Path(tmp))
        os.environ["REBAR_TRACKER_DIR"] = store  # ticket reads/writes hit the clone only
        os.environ["REBAR_SYNC_PUSH"] = "off"  # never push the throwaway edits anywhere
        for pid, perms in by_plan.items():
            for perm in sorted(perms, key=lambda r: r["permutation_index"]):
                pidx = perm["permutation_index"]
                attempts = list(prior.get((pid, pidx), []))
                if (
                    any(a.get("excluded") for a in attempts)
                    or any(not a.get("infra") for a in attempts)
                    or len(attempts) >= M.ATTEMPT_BUDGET
                ):
                    continue  # excluded / already has a kept verdict / budget-exhausted (resume)
                log.info("[B] %s perm %d", pid, pidx)
                while len(attempts) < M.ATTEMPT_BUDGET:
                    try:
                        rebar.edit_ticket(pid, description=perm["plan"])  # on the clone
                        verdict = plan_review.review_plan(
                            pid,
                            source="local",
                            repo_root=repo_root,
                            sign=False,
                            emit_sidecar=False,
                            force=True,
                        )
                    except Exception as exc:  # noqa: BLE001 — narrow: only ticket-not-found
                        if not _is_ticket_not_found(exc):
                            raise  # any other failure is a real bug — never silently pad
                        log.warning(
                            "[B] %s perm %d — ticket absent from clone (%s); EXCLUDED",
                            pid,
                            pidx,
                            exc,
                        )
                        att = _excluded_attempt(perm, len(attempts), "ticket_not_found")
                        _append_jsonl(B_RAW, att)
                        attempts.append(att)
                        break  # permanent condition — do not retry the vanished ticket
                    v = verdict.get("verdict")
                    cov = verdict.get("coverage") or {}
                    infra = M.is_infra_indeterminate_verdict(v, cov)
                    att = {
                        "plan_id": pid,
                        "permutation_index": pidx,
                        "section_order": perm["section_order"],
                        "attempt": len(attempts),
                        "verdict": v,
                        "fired_criteria": _fired_criteria(verdict),
                        "coverage_flags": {
                            "llm_unavailable": bool(cov.get("llm_unavailable")),
                            "verify_failed": bool(cov.get("verify_failed")),
                        },
                        "infra": infra,
                    }
                    _append_jsonl(B_RAW, att)
                    attempts.append(att)
                    if not infra:
                        break
        os.environ.pop(
            "REBAR_TRACKER_DIR", None
        )  # stop routing reads at the (about-to-vanish) clone
    real_head_after = _git_head(src)
    if real_head_after != real_head_before:
        raise RuntimeError(
            f"ISOLATION VIOLATION: the real tickets store HEAD moved during Exp B "
            f"({real_head_before} -> {real_head_after}) — the clone was not isolated"
        )
    log.info("isolation OK: real store HEAD unchanged (%s)", real_head_before)
    _materialize_exp_b()


def _materialize_exp_b() -> None:
    """Rebuild the final ``e6_ordershuffle.jsonl`` (one kept verdict per plan×permutation) and
    ``e6_ordershuffle_excluded.jsonl`` from the raw journal."""
    by_perm: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for att in _read_jsonl(B_RAW):
        by_perm[(att["plan_id"], att["permutation_index"])].append(att)
    results: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for (pid, pidx), attempts in sorted(by_perm.items()):
        # A vanished-ticket exclusion (recorded with `excluded`) takes precedence — it carries
        # `infra=False` and would otherwise be mis-read as a kept verdict below.
        exc_marker = next((a for a in attempts if a.get("excluded")), None)
        if exc_marker is not None:
            excluded.append(
                {
                    "plan_id": pid,
                    "permutation_index": pidx,
                    "n_attempts": len(attempts),
                    "reason": exc_marker.get("exclude_reason", "excluded"),
                }
            )
            continue
        kept = next((a for a in attempts if not a.get("infra")), None)
        if kept is None:
            excluded.append(
                {
                    "plan_id": pid,
                    "permutation_index": pidx,
                    "n_attempts": len(attempts),
                    "reason": "infra-indeterminate-cap",
                }
            )
            continue
        results.append(
            {
                "plan_id": pid,
                "permutation_index": pidx,
                "section_order": kept["section_order"],
                "verdict": kept["verdict"],
                "fired_criteria": kept["fired_criteria"],
            }
        )
    _write_jsonl(B_RESULTS, results)
    _write_jsonl(B_EXCLUDED, excluded)
    log.info("Exp B materialized: %d verdicts, %d excluded", len(results), len(excluded))


# ── analyze (NO LLM): agreement tables + summary ───────────────────────────────────────
def _container_ids() -> set[str]:
    corpus = json.loads(CORPUS_SAMPLE.read_text())
    return {rec["id"] for rec in corpus if rec.get("has_children")}


def analyze() -> None:
    """Compute the Exp A / Exp B agreement tables + ``e6_summary.json`` from the recorded votes."""
    summary: dict[str, Any] = {
        "experiment": "E6",
        "ticket": "a880-b7e1-dc3e-407c",
        "epic": "6982-2e75-f8fa-43b9",
    }

    # ---- Exp A: self-consistency over pass3 `decision` --------------------------------
    a_rows = _read_jsonl(A_RESULTS)
    by_finding: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in a_rows:
        by_finding[r["finding_id"]].append(r)
    a_ratings: list[list[str]] = []
    with A_AGREEMENT.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(
            [
                "finding_id",
                "criterion",
                "decision_1",
                "decision_2",
                "decision_3",
                "all_agree",
                "modal_decision",
            ]
        )
        for fid, votes in sorted(by_finding.items()):
            votes = sorted(votes, key=lambda v: v["vote"])
            decs = [v["decision"] for v in votes]
            if len(decs) != M.VOTE_TARGET:
                continue
            a_ratings.append(decs)
            w.writerow([fid, votes[0]["criterion"], *decs, len(set(decs)) == 1, M.modal(decs)])
    if a_ratings:
        agr = M.compute_agreement(a_ratings)
        sc = {
            "n_findings": len(a_ratings),
            **agr,
            "gates": "R5",
            "n_excluded": len(_read_jsonl(A_EXCLUDED)),
            "agreement_unit": "pass3 decision in {block,advisory,dropped,indeterminate}",
        }
        if not sc["pass"]:
            sc["blocker"] = (
                "R5 BLOCKER: self-consistency below floor "
                f"(kappa={sc['fleiss_kappa']}, raw={sc['raw_agreement']}; "
                "need kappa>=0.6 AND raw>=0.8)"
            )
        summary["self_consistency"] = sc
        log.info(
            "Exp A: kappa=%.3f raw=%.3f pass=%s (n=%d)",
            agr["fleiss_kappa"],
            agr["raw_agreement"],
            agr["pass"],
            len(a_ratings),
        )
    else:
        log.warning("Exp A: no votes recorded yet — run run-a first")

    # ---- Exp B: order-shuffle over gate `verdict` -------------------------------------
    b_rows = _read_jsonl(B_RESULTS)
    by_plan: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in b_rows:
        by_plan[r["plan_id"]].append(r)
    containers = _container_ids()
    b_ratings: list[list[str]] = []
    jaccards: list[float] = []
    container_ratings: list[list[str]] = []
    with B_AGREEMENT.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["plan_id", "verdict_1", "verdict_2", "verdict_3", "all_agree", "modal_verdict"])
        for pid, perms in sorted(by_plan.items()):
            perms = sorted(perms, key=lambda r: r["permutation_index"])
            verds = [r["verdict"] for r in perms]
            if len(verds) != N_PERMS:
                continue
            b_ratings.append(verds)
            if pid in containers:
                container_ratings.append(verds)
            sets = [set(r.get("fired_criteria") or []) for r in perms]
            pairs = [(0, 1), (0, 2), (1, 2)]
            jaccards.append(sum(M.jaccard(sets[i], sets[j]) for i, j in pairs) / len(pairs))
            w.writerow([pid, *verds, len(set(verds)) == 1, M.modal(verds)])
    # Honest N: total plans frozen into the inputs vs. the plans that yielded a full
    # N_PERMS reading. A plan whose ticket has since vanished from the tickets branch is
    # excluded (all its permutations recorded ticket_not_found) and is NOT padded over.
    total_plans = len({r["plan_id"] for r in _read_jsonl(B_INPUTS)})
    excluded_by_plan: dict[str, dict[str, Any]] = {}
    for row in _read_jsonl(B_EXCLUDED):
        e = excluded_by_plan.setdefault(
            row["plan_id"],
            {"plan_id": row["plan_id"], "reason": row["reason"], "n_perms_excluded": 0},
        )
        e["n_perms_excluded"] += 1
    if b_ratings:
        agr = M.compute_agreement(b_ratings)
        os_ = {
            "n_plans": len(b_ratings),
            "n_plans_total": total_plans,
            "n_plans_excluded": len(excluded_by_plan),
            "excluded_plans": sorted(excluded_by_plan.values(), key=lambda e: e["plan_id"]),
            **agr,
            "gates": "R3",
            "mean_fired_criteria_jaccard": round(sum(jaccards) / len(jaccards), 4),
            "agreement_unit": "verdict in {PASS,BLOCK,INDETERMINATE}",
            "container_subset_descriptive": {
                "n_plans": len(container_ratings),
                "raw_agreement": (
                    round(M.raw_agreement(container_ratings), 4) if container_ratings else None
                ),
                "note": "descriptive only, NOT gated (under-powered: see ticket a880 Exp B)",
            },
        }
        if not os_["pass"]:
            os_["blocker"] = (
                "R3 PREREQUISITE FAILURE: order-shuffle below floor "
                f"(kappa={os_['fleiss_kappa']}, raw={os_['raw_agreement']}; "
                "need kappa>=0.6 AND raw>=0.8)"
            )
        summary["order_shuffle"] = os_
        log.info(
            "Exp B: kappa=%.3f raw=%.3f pass=%s (n=%d)",
            agr["fleiss_kappa"],
            agr["raw_agreement"],
            agr["pass"],
            len(b_ratings),
        )
    else:
        log.warning("Exp B: no verdicts recorded yet — run run-b first")

    SUMMARY.write_text(json.dumps(summary, indent=2) + "\n")
    log.info("wrote %s", SUMMARY.name)
    print(json.dumps(summary, indent=2))  # noqa: T201 — machine-contract stdout


# ── CLI ────────────────────────────────────────────────────────────────────────────────
def _resolve_repo_root(arg: str | None) -> str:
    """The code-grounding checkout: ``--repo-root`` > ``$REBAR_E6_REPO_ROOT`` > the harness's own
    git top-level. Exp A's agentic verifier + Exp B's ``review_plan`` read code from here."""
    if arg:
        return os.path.abspath(arg)
    env = os.environ.get("REBAR_E6_REPO_ROOT")
    if env:
        return os.path.abspath(env)
    top = subprocess.run(
        ["git", "-C", str(HARNESS_DIR), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=True,
    )
    return top.stdout.strip()


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="E6 judge-reliability driver (ticket a880)")
    p.add_argument("command", choices=["build-inputs", "run-a", "run-b", "analyze", "all"])
    p.add_argument(
        "--repo-root",
        default=None,
        help="code-grounding checkout (default: $REBAR_E6_REPO_ROOT or git top-level)",
    )
    args = p.parse_args(argv)
    cmd = args.command

    if cmd in ("build-inputs", "all"):
        build_inputs_a()
        build_inputs_b()
    if cmd in ("run-a", "all"):
        run_a(_resolve_repo_root(args.repo_root))
    if cmd in ("run-b", "all"):
        run_b(_resolve_repo_root(args.repo_root))
    if cmd in ("analyze", "all"):
        analyze()


if __name__ == "__main__":
    main()
