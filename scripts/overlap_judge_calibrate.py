#!/usr/bin/env python3
"""Live calibration probe for the Stage-2 batched overlap judge (ticket d147-c219-baf9-4e9e).

The batch path is a PROMPT + CONTRACT artifact, so its behaviour cannot be pinned by a canned
runner: unit tests with a fake runner passed throughout the entire window in which the feature
surfaced zero findings in 2,093 real plan reviews. This script therefore makes LIVE model calls
(T5c calibration discipline, `docs/calibration/`) and asserts the two ends of the precision
contract at once:

  * a blatant near-duplicate pair SURFACES — non-abstain with `confidence >= overlap_conf_threshold`
    in BOTH orderings, and `aggregate` emits a finding;
  * an unrelated pair does NOT surface — the false-flag guard stays intact.

It also prints the measured serialized size of one verdict entry, which is the evidence behind
`_OUTPUT_TOKENS_PER_VERDICT` in `rebar.llm.overlap.judge`.

Run:  REBAR_LLM_BEDROCK_REGION=us-east-1 python scripts/overlap_judge_calibrate.py
Exit: 0 when every assertion holds, 1 otherwise.
"""

from __future__ import annotations

import json
import sys

from rebar.llm.config import LLMConfig
from rebar.llm.overlap.judge import (
    _CANDIDATES_PER_CALL,
    _batch_output_token_limit,
    aggregate,
    judge_batch,
)

# Two digests naming the SAME artifact with paraphrased scope — the near-duplicate the probe in
# the ticket found returning confidence=0.0. Kept deliberately unmistakable: the point of the
# case is the mechanism, not the model's discrimination at the margin.
QUERY = {
    "component_or_area": "src/rebar/llm/overlap/judge.py",
    "problem_keywords": ["overlap judge", "batch call", "output token limit", "truncation"],
    "key_entities": ["judge_batch", "output_token_limit", "_CANDIDATES_PER_CALL"],
    "propositions": [
        "judge_batch caps its response at a flat 1024 output tokens",
        "a full batch of candidate verdicts does not fit in that cap, so the whole batch abstains",
        "the cap must scale with the number of candidates in the batch",
    ],
}

NEAR_DUPLICATE = {
    "component_or_area": "src/rebar/llm/overlap/judge.py",
    "problem_keywords": ["batched overlap judging", "truncated response", "token budget"],
    "key_entities": ["judge_batch", "output_token_limit"],
    "propositions": [
        "the fixed 1024-token output budget in judge_batch truncates multi-candidate responses",
        "truncation makes every candidate in the batch abstain",
        "size the budget from the batch's candidate count instead of a constant",
    ],
}

UNRELATED = {
    "component_or_area": "docs/jira-mapping.md",
    "problem_keywords": ["jira", "documentation", "field mapping", "onboarding"],
    "key_entities": ["Jira custom field", "epic link", "docs/jira-mapping.md"],
    "propositions": [
        "the Jira field-mapping table omits the epic-link custom field id",
        "new operators cannot tell which Jira field rebar writes the epic link to",
        "document the field id alongside the other mapped fields",
    ],
}

CASES = [
    ("near_duplicate", NEAR_DUPLICATE, True),
    ("unrelated", UNRELATED, False),
]


def _run_case(name: str, candidate: dict, cfg: LLMConfig) -> tuple[bool, list[str]]:
    """Judge one candidate against QUERY in both orderings; return (ok, report lines)."""
    lines: list[str] = []
    batch = [(name, candidate)]
    r1 = judge_batch(QUERY, batch, cfg, None, shared_side="first")[name]
    r2 = judge_batch(QUERY, batch, cfg, None, shared_side="second")[name]
    lines.append(f"  ordering 1 (query FIRST):  {json.dumps(r1, sort_keys=True)}")
    lines.append(f"  ordering 2 (query SECOND): {json.dumps(r2, sort_keys=True)}")
    entry_bytes = max(len(json.dumps({**r1, "candidate_id": name})), 1)
    lines.append(f"  measured verdict-entry size: {entry_bytes} bytes of JSON")
    finding = aggregate("QUERY", name, r1, r2, cfg)
    lines.append(f"  aggregate -> {json.dumps(finding, sort_keys=True) if finding else 'None'}")
    return (finding is not None), lines


def _run_full_batch(cfg: LLMConfig) -> tuple[bool, list[str]]:
    """The PRODUCTION shape: a full ``_CANDIDATES_PER_CALL`` batch, the near-duplicate riding
    among distractors. This is the case the flat 1024-token cap truncated wholesale, so a probe
    that only ever judges one candidate at a time would not have caught the regression."""
    lines: list[str] = []
    batch = [("near_duplicate", NEAR_DUPLICATE)]
    for i in range(_CANDIDATES_PER_CALL - 1):
        distractor = dict(UNRELATED)
        distractor["propositions"] = [*UNRELATED["propositions"], f"distractor variant {i}"]
        batch.append((f"unrelated_{i}", distractor))
    budget = _batch_output_token_limit(len(batch))
    lines.append(f"  batch of {len(batch)}; derived output_token_limit = {budget}")
    r1 = judge_batch(QUERY, batch, cfg, None, shared_side="first")
    r2 = judge_batch(QUERY, batch, cfg, None, shared_side="second")
    ok = True
    for cid, _ in batch:
        finding = aggregate("QUERY", cid, r1[cid], r2[cid], cfg)
        surfaced = finding is not None
        want = cid == "near_duplicate"
        ok = ok and surfaced == want
        lines.append(
            f"  {cid}: o1={r1[cid]['relation']}/{r1[cid]['confidence']} "
            f"o2={r2[cid]['relation']}/{r2[cid]['confidence']} "
            f"surfaced={surfaced} (want {want})"
        )
    return ok, lines


def main() -> int:
    cfg = LLMConfig.from_env()
    threshold = cfg.overlap_conf_threshold
    print(f"overlap_conf_threshold = {threshold}\n")
    ok = True
    for name, candidate, want_surface in CASES:
        print(f"[{name}] expect surfaced={want_surface}")
        surfaced, lines = _run_case(name, candidate, cfg)
        for line in lines:
            print(line)
        verdict = "PASS" if surfaced == want_surface else "FAIL"
        ok = ok and surfaced == want_surface
        print(f"  => {verdict} (surfaced={surfaced})\n")

    print(f"[full_batch] expect ONLY near_duplicate surfaced, {_CANDIDATES_PER_CALL} candidates")
    batch_ok, lines = _run_full_batch(cfg)
    for line in lines:
        print(line)
    ok = ok and batch_ok
    print(f"  => {'PASS' if batch_ok else 'FAIL'}\n")

    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
