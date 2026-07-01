"""T5c trust-boundary recalibration: old rubric vs new rubric on real fixtures.

Positive: d251 (AWS Gerrit — internet-facing service, no human/admin auth).
Negatives: 8a1c / 4702 (local, in-process gate/attestation work — no network surface).

Runs the T5c Pass-1 finder in isolation via criterion_preview (one LLM call each),
NEW rubric via criterion_id="T5c", OLD rubric via an inline override.
"""

import json
import os

import rebar
from rebar.llm.plan_review import registry
from rebar.llm.workflow.criterion_preview import preview_criterion

REPO = os.getcwd()

OLD_T5C = (
    "OVERLAY — apply ONLY if the plan actually adds a security surface in THIS application's "
    "domain: a new endpoint, network exposure, an authn/authz boundary, storage/transmission of "
    "sensitive data, PII, or a credential/secret/grant. If the application has no such surface "
    "(e.g. a local library / CLI / git-backed tool with no network or auth), PASS as not-applicable. "
    "DERIVE the security model from the application's ACTUAL domain — do NOT import generic web-app "
    "concepts (e.g. a 'declared access level', endpoint authn) that this application does not have; "
    "a finding that imposes a security requirement the application's domain does not contain is a "
    "FALSE POSITIVE, not a gap. Where a real surface exists, check (OWASP only where the category "
    "applies): (a) sensitive paths use the app's own auth mechanism; (b) data protection — "
    "encryption at rest/in transit; (c) LEAST-PRIVILEGE on any new credential/role/grant; (d) SECRET "
    "LIFECYCLE — no plaintext secrets. SEVERITY priors: an undeclared sensitive surface or a "
    "plaintext secret is high. PASS if the application's actual security boundaries are explicit."
)

FIXTURES = {
    "d251 (positive: internet-facing Gerrit, no human auth)": "d251",
    "8a1c (negative: local signed-gate work, no network)": "8a1c",
    "4702 (negative: local attestation work, no network)": "4702",
}


def plan_text(tid: str) -> str:
    return rebar.show_ticket(tid)["description"]


def run(label: str, request: dict) -> dict:
    r = preview_criterion(request, repo_root=REPO, timeout=180.0)
    v = r.get("verdict")
    finding = (r.get("finding") or {})
    ftext = finding.get("finding") if isinstance(finding, dict) else finding
    print(f"  {label:6s} verdict={v!r} timed_out={r.get('timed_out', False)}")
    if ftext:
        print(f"         finding: {str(ftext)[:160]}")
    return {"verdict": v, "finding": ftext, "timed_out": r.get("timed_out", False)}


t5c_routing = registry.effective_routing(REPO).get("T5c")
results = {}
for label, tid in FIXTURES.items():
    print(f"\n### {label}")
    fixture = {"input": plan_text(tid), "filename": f"{tid}.md"}
    new = run("NEW", {"criterion_id": "T5c", "fixture": fixture})
    old = run(
        "OLD",
        {"inline": {"prompt": OLD_T5C, "routing": t5c_routing}, "fixture": fixture},
    )
    results[label] = {"old": old, "new": new}

print("\n\n=== SUMMARY (old -> new) ===")
for label, r in results.items():
    print(f"{label}\n   old={r['old']['verdict']}  ->  new={r['new']['verdict']}")

with open(os.path.join(os.environ["CLAUDE_JOB_DIR"], "tmp", "t5c_results.json"), "w") as f:
    json.dump(results, f, indent=2)
