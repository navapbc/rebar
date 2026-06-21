#!/usr/bin/env python3
"""Build criteria_v8.json from v7 — adopt the THREE-PASS review structure (epic 9da1 / log 9dba).

The user's directive: the plan-review gate adopts the three-pass standard, REPLACING per-criterion
model-emitted severity. v8 makes the registry three-pass-native:

  - severity_by="pass3": severity is NOT emitted by the Pass-1 reviewer; it is computed
    deterministically in Pass-3 from the Pass-2 verifier's severity ATTRIBUTES. The descriptor's
    prose "SEVERITY: ..." hints are retained only as priors for the verifier / the Pass-1 `impact`
    free-text; the Pass-1 tool has no severity field, so the model cannot emit one.
  - block_threshold + default_posture="advisory": the Pass-3 block|advisory decision is per-criterion
    and project-overridable; defaults start HIGH so almost everything is advisory while calibration
    data is gathered (9da1 AC). The DET floor (P1-P7, separate harness) is the only blocking tier.
  - trigger fix (E4-confirmed): T10/T11/T12 are LLM-ROUTED for PLAN review, not deterministic. E4
    showed the deterministic keyword over-fires on plans — rebar 'migration' = the bash->Python
    strangler (NOT data-migration), 'deploy'/'rollback' are ubiquitous: det T11 fired 2/19 (both
    false), T12 fired 7/19 (all false), LLM router fired 0 (correct). (On a code-review DIFF the
    file-glob triggers in the 9da1 catalog are high-precision; on a PLAN there is no diff, so route
    by LLM.)

Run:  python make_crit_v8.py   ;   python ../harnesses/check_registry_coverage.py criteria_v8.json
"""
import json, os

HERE = os.path.dirname(__file__)
V7 = json.load(open(os.path.join(HERE, "criteria_v7.json")))

# E4-confirmed: route these overlays by the LLM router on plans (deterministic keyword over-fires).
LLM_ROUTED = {"T10", "T11", "T12", "T8", "T6", "G6", "T9"}
LLM_TRIGGER_NOTE = {
    "T10": "infrastructure/IaC intent (LLM-routed for plans — deterministic IaC keywords over-fire on prose; "
           "on a code DIFF use the *.tf/Dockerfile/k8s file-glob triggers from the 9da1 IaC catalog instead)",
    "T11": "schema/data-shape change or backfill over PERSISTED data (LLM-routed — E4: deterministic 'migration' "
           "false-fires on rebar's bash->Python strangler; the LLM router correctly distinguishes data-migration)",
    "T12": "changes runtime behavior of a deployed/long-running system (LLM-routed — E4: deterministic "
           "'deploy'/'rollback' fired 7/19 plans, all false; LLM router fired 0)",
}

# Pass-3 per-criterion block thresholds. Start HIGH -> advisory (9da1: gather calibration data first).
# A criterion blocks only if Pass-3 confidence >= block_threshold AND computed severity is high enough.
# DEFAULT advisory for every LLM criterion in v1; the DET floor is the only hard blocker.
DEFAULT_BLOCK_THRESHOLD = 0.95


def main():
    out = []
    for c in V7:
        n = dict(c)
        n["severity_by"] = "pass3"          # severity computed deterministically downstream, not by Pass-1
        n["default_posture"] = "advisory"
        n["block_threshold"] = DEFAULT_BLOCK_THRESHOLD
        if c["id"] in LLM_ROUTED:
            n["overlay_routing"] = "llm"    # the relevance router decides applicability, not a keyword
            if c["id"] in LLM_TRIGGER_NOTE:
                n["trigger"] = LLM_TRIGGER_NOTE[c["id"]]
        elif c.get("routing") == "overlay":
            n["overlay_routing"] = "deterministic"
        out.append(n)

    path = os.path.join(HERE, "criteria_v8.json")
    json.dump(out, open(path, "w"), indent=1, ensure_ascii=False)
    llm = [c["id"] for c in out if c.get("overlay_routing") == "llm"]
    det = [c["id"] for c in out if c.get("overlay_routing") == "deterministic"]
    print(f"wrote {path}: {len(out)} descriptors (three-pass-native)")
    print(f"  severity_by=pass3 + default_posture=advisory on all {len(out)}")
    print(f"  overlay routing — LLM: {llm}")
    print(f"                  — deterministic: {det}")


if __name__ == "__main__":
    main()
