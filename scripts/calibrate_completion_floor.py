"""Completion-floor calibration: the Pass-2 completion sub-call vs the gold set (story 77cf).

Runs the REAL ``plan_review_completion`` sub-call over the frozen gold set
(``tests/unit/gold_set_completion.py``) against a synthetic partially-complete epic, then scores the
model's ``containment`` / ``layer`` answers — and the resulting Pass-3 floor decision — against the
gold labels. Because the changed artifact is a PROMPT, this is a LIVE LLM run (per G-Eval: freeze
wording, calibrate to a gold set). Prints per-axis + per-category agreement and Cohen's kappa on the
binary drop/keep decision; the human-written record lives at ``docs/calibration/completion_floor.md``.

Reproduce:
  REBAR_MCP_ALLOW_LLM=1 python scripts/calibrate_completion_floor.py
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

# the gold set lives with the tests (its other consumer is the deterministic e2e test)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests" / "unit"))
from gold_set_completion import CATEGORIES, DELIVERED_CHILD_IDS, GOLD_SET  # noqa: E402

from rebar.llm.config import LLMConfig  # noqa: E402
from rebar.llm.plan_review import passes  # noqa: E402
from rebar.llm.runner import get_runner  # noqa: E402

PROMPT_FILE = Path("src/rebar/llm/reviewers/plan_review_completion.md")
_PRESERVE = frozenset({"T5c", "T10"})
_FLOOR = 0.4

PLAN = (
    "# Epic: ingest-and-reconcile pipeline\n\n"
    "Delivered children: del-a (retry/backoff loop), del-b (record parser), del-c (results API + "
    "cache). Open sibling: op-x (downstream consumer, still in progress). One child was force-closed "
    "without verification: fc1.\n\n"
    "## Acceptance Criteria\n- [ ] children compose into one pipeline\n- [ ] op-x consumes del-c\n"
)
MANIFEST = [
    {"ticket_id": cid, "ac_text": f"- [ ] {cid} works\n- [ ] {cid} is verified"}
    for cid in sorted(DELIVERED_CHILD_IDS)
]


def _kappa(pairs: list[tuple[bool, bool]]) -> float:
    """Cohen's kappa for the binary drop/keep decision (gold vs model)."""
    n = len(pairs)
    if not n:
        return 0.0
    po = sum(1 for g, m in pairs if g == m) / n
    pg = sum(1 for g, _ in pairs if g) / n
    pm = sum(1 for _, m in pairs if m) / n
    pe = pg * pm + (1 - pg) * (1 - pm)
    return 1.0 if pe == 1.0 else round((po - pe) / (1 - pe), 3)


def main() -> int:
    prompt_hash = hashlib.sha256(PROMPT_FILE.read_bytes()).hexdigest()[:12]
    cfg = LLMConfig.from_env(repo_root=os.getcwd())
    runner = get_runner(cfg)
    findings = [dict(c.finding) for c in GOLD_SET]

    print(f"prompt: {PROMPT_FILE} sha256:{prompt_hash}")
    print(f"model: {cfg.model}  cases: {len(GOLD_SET)}\n")

    out = passes.pass2_completion(
        runner, cfg, plan=PLAN, findings=findings, delivered_manifest=MANIFEST
    )
    if not out:
        print("ERROR: sub-call returned nothing (degraded). Calibration could not run.")
        return 1

    cont_ok = layer_ok = 0
    decision_pairs: list[tuple[bool, bool]] = []
    per_cat: dict[str, list[int]] = {c: [0, 0] for c in CATEGORIES}  # [correct_decision, total]
    misses: list[str] = []
    for i, case in enumerate(GOLD_SET):
        ans = out.get(i, {})
        cont_ok += ans.get("containment") == case.gold["containment"]
        layer_ok += ans.get("layer") == case.gold["layer"]
        model_drop = passes.completion_floor_drop(
            ans,
            0.1,
            case.finding["criteria"],
            floor=_FLOOR,
            preserve=_PRESERVE,
            delivered_ids=DELIVERED_CHILD_IDS,
        )
        decision_pairs.append((case.expect_drop, model_drop))
        per_cat[case.category][1] += 1
        if model_drop == case.expect_drop:
            per_cat[case.category][0] += 1
        else:
            misses.append(
                f"  MISS {case.id} [{case.category}] gold_drop={case.expect_drop} "
                f"model_drop={model_drop} model={ans.get('containment')}/{ans.get('layer')}"
            )

    n = len(GOLD_SET)
    print(f"containment agreement: {cont_ok}/{n} = {cont_ok / n:.0%}")
    print(f"layer agreement:       {layer_ok}/{n} = {layer_ok / n:.0%}")
    decision_ok = sum(1 for g, m in decision_pairs if g == m)
    print(f"floor decision match:  {decision_ok}/{n} = {decision_ok / n:.0%}")
    print(f"Cohen's kappa (drop/keep): {_kappa(decision_pairs)}\n")
    print("per category (correct floor decision / total):")
    for c in CATEGORIES:
        ok, tot = per_cat[c]
        print(f"  {c:18s} {ok}/{tot}")
    # the safety-critical metric: NO must-never-suppress anchor may be wrongly dropped
    wrong_drops = [g_m for g_m in decision_pairs if g_m == (False, True)]
    print(f"\nmust-never-suppress violations (kept-required but dropped): {len(wrong_drops)}")
    if misses:
        print("\n".join(misses))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
