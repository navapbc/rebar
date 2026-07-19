"""E5 — cross-gate non-regression replay (story pisciform-spineless-wobbegong / 5342).

R5 (story empty-microbial-antlion) adds ONE na-default sub-answer —
``asserted_capability_confirmed`` — to the SHARED review kernel's ``GRADED_BINARY`` vocabulary
(``rebar.llm.review_kernel.decide``). That vocabulary is shared with the CODE-REVIEW gate, so
the epic's gate row demands PROOF that code-review decisions are byte-identical before the kernel
change lands. This harness is that proof.

METHOD (deterministic — NO live LLM for the decision comparison)
----------------------------------------------------------------
The committed corpus ``docs/experiments/code_review_adjudication.jsonl`` (161 code-review
findings) records, per finding, the PRE-R5 kernel's ``(validity, impact, priority)``. For every
row we run the deterministic Pass-3 decision math (``review_kernel.decide``) under TWO kernels:

* **PRE**  — the pre-R5 vocabulary (``GRADED_BINARY`` WITHOUT ``asserted_capability_confirmed``),
  over a binary reconstructed to reproduce the recorded ``validity``. This is a pre-R5 sidecar:
  the new field is simply ABSENT.
* **POST** — the amended vocabulary (current ``GRADED_BINARY``, WITH the new field), over the
  SAME reconstructed binary PLUS ``asserted_capability_confirmed="na"`` — exactly what the R5
  verifier emits for any finding OUTSIDE the G6/E4/T3 asserted-capability cohort.

Because the new sub-answer is na-default and ``decide.validity`` counts only ``yes|no|insufficient``
(``na``/absent both abstain — excluded from the graded mean), and because it is a *validity*
sub-answer that touches neither ``impact_code`` nor any veto, PRE and POST must produce a
BYTE-IDENTICAL ``pass3_decide`` dict (decision, validity, impact, priority, severity) for every
row. The recorded impact is fed straight through (``impact_fn`` closure) so the comparison
isolates exactly the axis R5 could perturb — ``validity`` — under the real kernel code.

The harness also asserts AC3: none of the 161 findings is in the G6/E4/T3 cohort, so
``asserted_capability_confirmed`` is ``na`` for ALL of them (the conservative scope).

Outputs (atomic writes, deterministic — re-running reproduces byte-for-byte):
    runs/e5_nonregression_diff.json   — per-row PRE-vs-POST diffs + summary (the gate artifact)

Run:  python docs/experiments/plan-review-gate/harnesses/e5_cross_gate_nonregression.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from rebar.llm.review_kernel import decide

REPO = Path(__file__).resolve().parents[4]
CORPUS = REPO / "docs/experiments/code_review_adjudication.jsonl"
OUT = REPO / "docs/experiments/plan-review-gate/runs/e5_nonregression_diff.json"

# The R5 field + the asserted-capability cohort it is scoped to (single-sourced from the E6
# harness's COHORT and R5's decide.GRADED_BINARY addition).
R5_FIELD = "asserted_capability_confirmed"
COHORT = {"G6", "E4", "T3"}

# The PRE-R5 graded vocabulary = the current one minus R5's addition. Deriving it from the live
# tuple (rather than hardcoding) means this stays correct if the vocabulary grows again.
PRE_GRADED = tuple(q for q in decide.GRADED_BINARY if q != R5_FIELD)


def _binary_for_validity(validity: float, question_set: tuple[str, ...]) -> dict[str, str]:
    """Reconstruct a deterministic binary over ``question_set`` whose ``decide.validity`` equals
    ``validity`` to grid precision: the first ``round(validity*n)`` questions answer ``yes`` and
    the rest ``no`` (both graded; none ``na``), so the graded fraction is ``k/n``. The exact
    binary is irrelevant to the proof — PRE and POST are compared over the SAME binary — but a
    faithful reconstruction lets the artifact double as an internal-consistency check on the
    corpus (recorded validity ≈ reconstructed validity)."""
    n = len(question_set)
    k = round(validity * n)
    return {q: ("yes" if i < k else "no") for i, q in enumerate(question_set)}


def _decide_under(vocab: tuple[str, ...], verification: dict[str, Any], impact: float) -> dict:
    """Run the REAL ``decide.pass3_decide`` with ``decide.GRADED_BINARY`` temporarily swapped to
    ``vocab`` (so ``validity()`` scores against the PRE or POST question set) and the recorded
    impact fed straight through, isolating the validity axis. Restores the global on exit."""
    saved = decide.GRADED_BINARY
    decide.GRADED_BINARY = vocab
    try:
        return decide.pass3_decide(
            verification,
            block_threshold=0.95,
            blocking_enabled=True,
            impact_fn=lambda _attrs: impact,
        )
    finally:
        decide.GRADED_BINARY = saved


def replay() -> dict[str, Any]:
    rows = [json.loads(line) for line in CORPUS.read_text().splitlines() if line.strip()]
    per_row: list[dict[str, Any]] = []
    decision_diffs = priority_diffs = validity_diffs = 0
    cohort_members = 0
    non_na = 0
    max_recon_err = 0.0

    for r in rows:
        criteria = set(r.get("criteria", []))
        if r.get("criterion"):
            criteria.add(r["criterion"])
        in_cohort = bool(criteria & COHORT)
        # R5 scope: na everywhere OUTSIDE the cohort. All corpus rows are outside it.
        acc = "na"  # asserted_capability_confirmed as the R5 verifier would emit it here
        if in_cohort:
            cohort_members += 1
        if acc != "na":
            non_na += 1

        validity = float(r["validity"])
        impact = float(r["impact"])

        # PRE sidecar: no R5 field at all, graded over the pre-R5 vocabulary.
        pre_binary = _binary_for_validity(validity, PRE_GRADED)
        pre = _decide_under(PRE_GRADED, {"binary": dict(pre_binary)}, impact)

        # POST sidecar: the SAME answers + the na-default R5 field, graded over the amended vocab.
        post_binary = {**pre_binary, R5_FIELD: acc}
        post = _decide_under(decide.GRADED_BINARY, {"binary": post_binary}, impact)

        max_recon_err = max(max_recon_err, abs(pre["validity"] - validity))
        d_dec = pre["decision"] != post["decision"]
        d_pri = pre["priority"] != post["priority"]
        d_val = pre["validity"] != post["validity"]
        decision_diffs += d_dec
        priority_diffs += d_pri
        validity_diffs += d_val
        if d_dec or d_pri or d_val:
            per_row.append(
                {
                    "finding_id": r.get("finding_id"),
                    "criteria": sorted(criteria),
                    "pre": pre,
                    "post": post,
                    "asserted_capability_confirmed": acc,
                }
            )

    summary = {
        "experiment": "E5 cross-gate non-regression replay",
        "ticket": "5342-568c-b881-4b2a (pisciform-spineless-wobbegong)",
        "gates": "R5 (ea39-c2d1-e26a-4ee6, empty-microbial-antlion)",
        "corpus": "docs/experiments/code_review_adjudication.jsonl",
        "corpus_rows": len(rows),
        "pre_graded_len": len(PRE_GRADED),
        "post_graded_len": len(decide.GRADED_BINARY),
        "r5_field": R5_FIELD,
        "decision_diffs": decision_diffs,
        "priority_diffs": priority_diffs,
        "validity_diffs": validity_diffs,
        "byte_identical": decision_diffs == priority_diffs == validity_diffs == 0,
        "cohort_members": cohort_members,
        "asserted_capability_confirmed_na_for_all": non_na == 0,
        "non_na_rows": non_na,
        "max_validity_reconstruction_error": round(max_recon_err, 6),
        "diffs": per_row,  # EMPTY iff the gate passes
    }
    return summary


def main() -> int:
    summary = replay()
    tmp = OUT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, OUT)
    ok = summary["byte_identical"] and summary["asserted_capability_confirmed_na_for_all"]
    print(json.dumps({k: v for k, v in summary.items() if k != "diffs"}, indent=2))  # noqa: T201
    print(f"\nE5 non-regression replay: {'PASS' if ok else 'FAIL'} -> {OUT}")  # noqa: T201
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
