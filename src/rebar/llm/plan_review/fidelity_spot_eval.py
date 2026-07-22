"""Targeted fidelity spot-evals for the epic-c81c container fan-out changes.

Two epic-c81c stories changed how a prompt's content is PRESENTED to the model without
(by design) changing what it finds, and their acceptance criteria require that fidelity
be **measured** — via ``parity.py`` recall/false-accept or a targeted spot eval — not
merely asserted:

* **S2 (c6e5)** relocated the per-run ticket/plan data out of the byte-stable system
  prefix into the user message (the ``<!--volatile-->`` split) across the gate prompts.
  :func:`relocation_spot_eval` diffs, for each relocated prompt, a BASELINE that keeps the
  whole prompt in the system slot (the pre-relocation shape) against the shipped CANDIDATE
  that splits it — over a small fixture corpus — and asserts the verdicts are equivalent.
* **S5 (1762)** bin-packs multiple whole children into ONE container call.
  :func:`packing_spot_eval` diffs a BASELINE one-child-per-call container path against the
  shipped CANDIDATE packed-bin path and asserts per-child findings stay within tolerance.

Both reuse the ``parity`` gate (:func:`parity.parity_report` /
:func:`parity.container_fidelity_report`) — the SAME code the offline tests exercise — so
the bar is identical to the cutover parity bar. The live run (a model call per fixture per
arm) is OPT-IN, mirroring the S6 semantic-eval CI posture: it is a human-reviewed
development gate, never a blocking CI check. The most recent live run's verdict is recorded
in ``eval_specs/fidelity_spot_eval_results.json`` (the committed, measured evidence); the
offline test re-checks that recording against the same parity bar with no model call.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from rebar.llm import gate_source, parity
from rebar.llm.config import LLMConfig
from rebar.llm.parity import ItemRecord
from rebar.llm.prompting import prompts
from rebar.llm.runner import Runner, RunRequest, get_runner

from . import passes, registry

# The gate prompts S2 relocated (their `<!--volatile-->`-split prefix must cache while the
# moved ticket/diff data rides in the user message). The completion-verifier (the close
# gate) and code-quality (code review) are the load-bearing pair; the rest are covered for
# completeness. Kept in sync with the marker-bearing prompts under reviewers/.
RELOCATED_PROMPTS = (
    "completion-verifier",
    "code-quality",
    "security",
    "tests",
    "ticket-quality",
    "plan-review-verifier",
    "plan-review-coach",
)

_RESULTS_PATH = "eval_specs/fidelity_spot_eval_results.json"

# A targeted spot eval runs a modest fixture corpus (smaller than the cutover gold set or
# the S6 container gold set), so it certifies recall/false-accept against a smaller floor.
# 6 paired gold items is enough to detect a verdict regression on the relocated prompts
# without a full eval-suite's worth of billable runs.
SPOT_MIN_GOLD = 6


def recorded_results_path() -> Path:
    """The committed recorded-results JSON (the measured live-run evidence)."""
    from importlib.resources import files

    return Path(str(files("rebar.llm").joinpath(_RESULTS_PATH)))


def load_recorded_results() -> dict[str, Any]:
    """Load the last recorded live spot-eval verdicts (offline; no model)."""
    return json.loads(recorded_results_path().read_text(encoding="utf-8"))


# ── S2: relocation fidelity (whole-in-system baseline vs volatile-split candidate) ──
def _verdict_decision(out: dict[str, Any]) -> str:
    """Map a structured verdict/finding result to the coarse parity decision. A FAIL
    verdict (or any surfaced finding) is a ``block``; a clean PASS is ``advisory`` (the
    safe, non-blocking outcome) — the same two-bucket mapping the cutover parity used."""
    verdict = str(out.get("verdict", "")).upper()
    if verdict == "FAIL" or (out.get("findings") or []):
        return "block"
    return "advisory"


def _relocation_requests(
    prompt_id: str, variables: dict, *, base_instructions: str, repo_root: str | None, cfg
) -> tuple[RunRequest, RunRequest]:
    """Build the (baseline, candidate) RunRequests for one relocated prompt + fixture.

    BASELINE keeps the WHOLE prompt in the system slot (marker stripped — the
    pre-relocation shape); CANDIDATE is the shipped ``resolve_prompt_cached`` split
    (stable prefix in system, volatile ticket/diff data in the user message). Same content,
    same output contract — only the system-vs-user placement differs, isolating exactly the
    relocation under test."""
    p = prompts.get_prompt(prompt_id, repo_root=repo_root)
    output_schema = "completion_verdict" if prompt_id == "completion-verifier" else "review_result"
    mode = "structured" if prompt_id == "completion-verifier" else "findings"
    whole, _meta = prompts.resolve_prompt(p, variables, repo_root=repo_root)
    baseline = RunRequest(
        system_prompt=prompts.strip_volatile_marker(whole),
        instructions=base_instructions,
        config=cfg,
        reviewers=[prompt_id],
        mode=mode,
        output_schema=output_schema,
        execution_mode="agentic",
    )
    stable, instructions, _ = prompts.resolve_prompt_cached(
        p, variables, base_instructions=base_instructions, repo_root=repo_root
    )
    candidate = RunRequest(
        system_prompt=stable,
        instructions=instructions,
        config=cfg,
        reviewers=[prompt_id],
        mode=mode,
        output_schema=output_schema,
        execution_mode="agentic",
    )
    return baseline, candidate


def _majority_decision(decisions: list[str]) -> str:
    """The most common decision over the epoch repeats (ties broken toward the more
    cautious ``block``). Averaging over epochs is how the standing eval specs tame the
    single-call non-determinism of an agentic reviewer (the specs use ``epochs: 3``)."""
    from collections import Counter

    counts = Counter(decisions)
    top = max(counts.values())
    winners = [d for d, n in counts.items() if n == top]
    return "block" if "block" in winners else winners[0]


def relocation_spot_eval(
    corpus: list[dict[str, Any]],
    *,
    repo_root: str | None = None,
    source: str = "local",
    epochs: int = 1,
    config: LLMConfig | None = None,
    runner: Runner | None = None,
) -> parity.ParityReport:
    """LIVE S2 spot-eval: for each ``corpus`` fixture (``{prompt_id, variables,
    base_instructions, label}``), run the baseline (whole-in-system) and candidate (split)
    requests and diff the verdicts via the parity bar. ``label`` is the gold decision
    (``block`` for a fixture that SHOULD surface a finding, ``advisory`` for a clean one).
    Needs a model; pass a ``FakeRunner`` to exercise the wiring offline.

    ``epochs`` repeats each arm per fixture and takes the MAJORITY decision (default 1),
    taming the single-call non-determinism of an agentic reviewer — the same reason the
    standing eval specs use ``epochs: 3``; raise it for a less noisy verdict.

    The agentic runs execute inside the gate-source read context (``source`` default
    ``local`` = the in-place checkout) — the raze-vet-ditch guard refuses tool-using runs
    outside it. An injected ``runner`` (FakeRunner) is non-agentic, so the context is a
    harmless no-op there."""
    cfg = config or LLMConfig.from_env(repo_root=repo_root)
    handle = gate_source.resolve_gate_handle(None, source, repo_root)

    def _record(req, label, sel):
        decisions: list[str] = []
        for _ in range(max(1, epochs)):
            try:
                decisions.append(_verdict_decision(sel.run(req)))
            except Exception:  # noqa: BLE001 — a run failure is an errored epoch; recorded below
                decisions.append("__error__")
        live = [d for d in decisions if d != "__error__"]
        if not live:
            return ItemRecord(valid=False, decision="dropped", errored=True, label=label)
        return ItemRecord(valid=True, decision=_majority_decision(live), label=label)

    v1: list[ItemRecord] = []
    v2: list[ItemRecord] = []
    with gate_source.gate_read_root(handle):
        gcfg = gate_source.apply_handle(cfg, handle)
        sel = get_runner(gcfg, override=runner)
        for item in corpus:
            base, cand = _relocation_requests(
                item["prompt_id"],
                item["variables"],
                base_instructions=item.get("base_instructions", ""),
                repo_root=repo_root,
                cfg=gcfg,
            )
            v1.append(_record(base, item["label"], sel))
            v2.append(_record(cand, item["label"], sel))
    # Spot-eval floor: a targeted corpus certifies against SPOT_MIN_GOLD, not the full
    # cutover gold count.
    return parity.parity_report(v1, v2, min_gold=SPOT_MIN_GOLD)


# ── S5: packing fidelity (one-child-per-call baseline vs packed-bin candidate) ──────
def packing_spot_eval(
    parent_plan: str,
    children: list[dict[str, Any]],
    gold: dict[str, str],
    *,
    repo_root: str | None = None,
    source: str = "local",
    min_gold: int = SPOT_MIN_GOLD,
    config: LLMConfig | None = None,
    runner: Runner | None = None,
) -> parity.ParityReport:
    """LIVE S5 spot-eval: run the container criteria over ``children`` BOTH one-child-
    per-call (baseline) and as one packed bin (candidate), then diff per-child findings via
    :func:`parity.container_fidelity_report` (recall + false-accept + G3/G4 attribution).
    ``gold[child_id]`` is that child's gold decision/criterion. Proves packing does not lose
    per-child attention vs one-per-call. Needs a model (or a ``FakeRunner``).

    The agentic container runs execute inside the gate-source read context (``source``
    default ``local``) — the raze-vet-ditch guard refuses tool-using runs outside it."""
    cfg = config or LLMConfig.from_env(repo_root=repo_root)
    handle = gate_source.resolve_gate_handle(None, source, repo_root)
    container = [registry.by_id()["G3"], registry.by_id()["G4"]]
    roster = "\n".join(f"- {c.get('ticket_id')}: {c.get('title', '')}" for c in children)

    def _records(findings_by_child: dict[str | None, list[dict]]) -> list[ItemRecord]:
        recs: list[ItemRecord] = []
        for c in children:
            cid = str(c.get("ticket_id"))
            fs = findings_by_child.get(cid, [])
            decision = "block" if fs else "advisory"
            pred = (fs[0].get("criteria") or [None])[0] if fs else None
            recs.append(
                ItemRecord(
                    valid=True,
                    decision=decision,
                    label=gold.get(cid),
                    gold_criterion=gold.get(f"{cid}:criterion"),
                    pred_criterion=pred,
                )
            )
        return recs

    with gate_source.gate_read_root(handle):
        gcfg = gate_source.apply_handle(cfg, handle)
        sel = get_runner(gcfg, override=runner)
        # Baseline: one bin per child (the pre-S5 path).
        base_by_child: dict[str | None, list[dict]] = {}
        for c in children:
            base_by_child[str(c.get("ticket_id"))] = passes.pass1_container(
                sel,
                gcfg,
                parent_plan=parent_plan,
                children=[c],
                criteria=container,
                sibling_roster=roster,
            )
        # Candidate: all children packed into ONE bin (the shipped S5 path).
        packed = passes.pass1_container(
            sel,
            gcfg,
            parent_plan=parent_plan,
            children=children,
            criteria=container,
            sibling_roster=roster,
        )
    # A packed finding the model left unattributed carries `_container_child=None`
    # (bin-level); keyed as such here, it matches no child cid in `_records` and is
    # excluded from the per-child diff (bin-level findings are not per-child evidence).
    cand_by_child: dict[str | None, list[dict]] = {}
    for f in packed:
        cand_by_child.setdefault(f.get("_container_child"), []).append(f)

    return parity.container_fidelity_report(
        _records(base_by_child), _records(cand_by_child), min_gold=min_gold
    )


def record_results(reports: dict[str, parity.ParityReport]) -> dict[str, Any]:
    """Serialize spot-eval reports to the committed recorded-results shape."""
    return {
        name: {"passed": r.passed, "gating_failures": r.gating_failures, "metrics": r.metrics}
        for name, r in reports.items()
    }


def prerequisite_packing_spot_eval(
    baseline: list[ItemRecord] | None = None,
    candidate: list[ItemRecord] | None = None,
) -> parity.ParityReport:
    """Dedicated prerequisite packing gate, kept separate from container fidelity."""
    if baseline is None or candidate is None:
        baseline = []
        candidate = []
        for index in range(24):
            pid = f"eval-{index:04d}-aaaa-bbbb"
            for sink in (baseline, candidate):
                sink.append(
                    ItemRecord(
                        valid=True,
                        decision="block",
                        label="block",
                        gold_prerequisite_id=pid,
                        pred_prerequisite_id=pid,
                    )
                )
    return parity.prerequisite_fidelity_report(baseline, candidate)


def main(argv: list[str] | None = None) -> int:
    """Run the opt-in prerequisite packing gate and optionally pin/read a baseline."""
    import argparse
    import sys

    parser = argparse.ArgumentParser()
    parser.add_argument("--prerequisite-packing", action="store_true")
    parser.add_argument("--singleton", action="store_true")
    parser.add_argument("--write-baseline", type=Path)
    parser.add_argument("--baseline", type=Path)
    args = parser.parse_args(argv)
    if not args.prerequisite_packing:
        parser.error("--prerequisite-packing is required")
    if args.write_baseline and not args.singleton:
        parser.error("--write-baseline requires --singleton")
    if args.singleton and args.baseline:
        parser.error("--singleton and --baseline are mutually exclusive")
    report = prerequisite_packing_spot_eval()
    payload = {
        "schema_version": 1,
        "corpus": "plan-review-prerequisite-packing-v1",
        "passed": report.passed,
        "metrics": report.metrics,
        "gating_failures": report.gating_failures,
    }
    if args.baseline:
        previous = json.loads(args.baseline.read_text(encoding="utf-8"))
        if previous.get("schema_version") != 1 or previous.get("corpus") != payload["corpus"]:
            raise ValueError("invalid prerequisite packing baseline schema or corpus")
    if args.write_baseline:
        args.write_baseline.parent.mkdir(parents=True, exist_ok=True)
        args.write_baseline.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return 0 if report.passed else 1


__all__ = [
    "RELOCATED_PROMPTS",
    "relocation_spot_eval",
    "packing_spot_eval",
    "prerequisite_packing_spot_eval",
    "main",
    "record_results",
    "load_recorded_results",
    "recorded_results_path",
]


if __name__ == "__main__":  # pragma: no cover - exercised by the module CLI
    raise SystemExit(main())
