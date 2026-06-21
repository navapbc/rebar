#!/usr/bin/env python3
"""Completeness guard: assert an implemented criteria set covers the canonical v4 §5 registry.

The criteria got silently dropped because the registry lived only as compressed prose
(design-of-record §5) with no enumerated list to diff an implementation against. This is
that missing checklist: the FULL v4 §5 registry is encoded here (one entry per criterion);
the script fails loudly if any criterion is absent from a criteria_*.json, so a subset can
never again pass unnoticed.

Usage: python check_registry_coverage.py [criteria_v4.json]
"""
import json, sys, os

# --- Canonical v4 §5 registry (verbatim from the design-of-record, session log 63cc) ---
REGISTRY = {
    "DET (Layer-1, blocks)": ["P1", "P2", "P3", "P4", "P5", "P6", "P7"],
    "Layer-2 judgment": ["F1", "F4", "E1", "E2", "E3", "E5", "E6", "G1G2", "G3", "G4", "E4", "A1"],
    # G5 (decomposition, from v3) is carried as an additional judgment criterion
    "Layer-2 judgment (v3 add)": ["G5", "G6"],
    # ISF (intent-source fidelity) — approved this session; single-turn/2-STEP, fed the linked session log
    # + ticket graph (NOT agent); fires only when a session log is linked.
    "Layer-2 judgment (v8 add)": ["ISF"],
    "Triggered overlays": ["T1", "T2", "T3", "T4", "T5a", "T5b", "T5c", "T5d", "T5e", "T6", "T7", "T8", "T9", "T10", "T11", "T12"],
    "Cross-cutting": ["COH", "BROAD"],   # coherence pass + bounded broad open-ended pass
}
# DET is a deterministic/code tier and not expected in the LLM criteria JSON; overlays/judgment/cross-cut are.
LLM_EXPECTED = set(REGISTRY["Layer-2 judgment"]) | set(REGISTRY["Layer-2 judgment (v3 add)"]) \
    | set(REGISTRY["Layer-2 judgment (v8 add)"]) \
    | set(REGISTRY["Triggered overlays"]) | set(REGISTRY["Cross-cutting"])
# AGENT tier = code-grounding only (grep/read the repo). E4/G1G2/A1 own code-grounding; G6 grounds
# mechanism-correctness in code. G3/G4 are NOT agent — they are ticket-analysis (parent ACs vs child
# tickets), reclassified to single-turn (the agent-vs-single-turn bright line: only criteria that must
# probe the live codebase/environment are agent-tier).
AGENT_TIER = {"G1G2", "E4", "A1", "G6"}
BROAD_TIER = {"BROAD"}

def main():
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", "criteria", "criteria_v6.json")
    have = {c["id"] for c in json.load(open(path))}
    single_turn_expected = LLM_EXPECTED - AGENT_TIER - BROAD_TIER
    missing = sorted(single_turn_expected - have)
    extra = sorted(have - LLM_EXPECTED)
    print(f"criteria file: {path}")
    print(f"  single-turn/overlay criteria present: {len(have & single_turn_expected)}/{len(single_turn_expected)}")
    print(f"  agent-tier (separate harness): {sorted(AGENT_TIER)}")
    if extra:
        print(f"  NOTE extra ids not in registry: {extra}")
    if missing:
        print(f"\nFAIL — these v4 §5 criteria are MISSING from the set: {missing}")
        print("A subset of the registry must never ship silently. Add them or justify the exclusion in the registry.")
        sys.exit(1)
    print("\nPASS — the criteria set covers every single-turn/overlay criterion in the canonical v4 §5 registry.")

if __name__ == "__main__":
    main()
