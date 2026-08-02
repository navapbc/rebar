"""Provider parity: Bedrock (v2) vs direct Anthropic (v1) on the standing eval corpus.

An opt-in, OPERATOR-RUN harness — never a CI job — that measures whether routing gate traffic
through Bedrock is non-inferior to the direct Anthropic path, against the bar
:func:`rebar.llm.parity.parity_report` already defines (structured-output validity, verdict
agreement with ZERO decision-level flips on the gold set, recall and false-accept within 2pp,
runtime error rate, and a gold-coverage floor). It is modelled on the landed spot-eval precedent
:mod:`rebar.llm.plan_review.fidelity_spot_eval`: same parity bar, same epoch-majority
aggregation, same committed-results-plus-offline-recheck shape, and the same
``_verdict_decision`` / ``_majority_decision`` mappings (imported, not re-stated, so the two
harnesses stay comparable).

Exactly one variable separates the arms. Both run the SAME cases through the SAME
:func:`rebar.llm.evals.eval_solver.run_case` path inside a :func:`rebar.llm.config.gate_config`
scope; only the model string differs. That scope is load-bearing rather than incidental:
``runner._effective_config`` substitutes ``req.config.model`` over the runner's own base config,
so a differently-configured runner alone would not flip the model.

The corpus POOLS gold-labelled dataset cases across eval specs, because no single spec's gold
set reaches ``parity.MIN_GOLD_ITEMS``. A case is eligible when its ``expect`` is gold-labelling
and its solver id RESOLVES to a non-agentic ``run_case`` arm — resolution, not merely "is not one
of the three agentic reviewers": an id that resolves to no arm raises at run time and would be
scored as an error rather than a verdict. The three agentic disposable-store reviewers are
excluded because they resolve their config through ``LLMConfig.from_env`` rather than
``resolve_gate_config``, so ``gate_config`` does not reach them and both arms would read the same
ambient model.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rebar.llm import parity
from rebar.llm.bedrock_model import DEFAULT_BEDROCK_MODEL_ID
from rebar.llm.config import DEFAULT_MODEL, VERIFIER_DEFAULT_MODEL, LLMConfig, gate_config
from rebar.llm.parity import ItemRecord

# Single-sourced from the spot-eval precedent so both harnesses score verdicts identically; a
# local copy would make their recorded evidence incomparable.
from rebar.llm.plan_review.fidelity_spot_eval import (
    _majority_decision as majority_decision,
)
from rebar.llm.plan_review.fidelity_spot_eval import (
    _verdict_decision as verdict_decision,
)

__all__ = [
    "AGENTIC_SOLVERS",
    "AUTH_VALIDATION",
    "CLASS_SLOTS",
    "DEFAULT_EPOCHS",
    "GENERIC",
    "SAMPLING_PARAMETER",
    "ArmOutcome",
    "CorpusItem",
    "classify_error",
    "corpus_digest",
    "eligible_cases",
    "load_recorded_results",
    "main",
    "non_bedrock_calls",
    "probe_slot",
    "recheck_recorded",
    "recorded_results_path",
    "run_arm",
    "run_slot",
    "select_corpus",
    "solver_arm",
    "unmeasured_slot",
    "usage_model_tally",
]

#: The three agentic disposable-store reviewers. They dispatch to ops that resolve
#: ``config or LLMConfig.from_env(...)`` directly, so ``gate_config`` does not reach them and the
#: provider split would silently collapse on any corpus item routed to one.
AGENTIC_SOLVERS = frozenset({"completion-verifier", "ticket-quality", "spec-alignment"})

#: ``expect`` values that carry a gold label, and the coarse parity decision each maps to — the
#: same two-bucket mapping :func:`verdict_decision` produces from a live result.
GOLD_EXPECTS = {"finding": "block", "fail": "block", "pass": "advisory"}

#: The paired arms per model-class slot: ``(v1 direct Anthropic, v2 Bedrock)``, same underlying
#: model on both sides. Production uses both classes — frontier is the Pass-1 finder, standard is
#: Pass-2 verify and Pass-4 coach — so certifying only one leaves the other unmeasured.
CLASS_SLOTS: dict[str, tuple[str, str]] = {
    "frontier": (DEFAULT_MODEL, "bedrock:us.anthropic.claude-opus-4-8"),
    "standard": (VERIFIER_DEFAULT_MODEL, f"bedrock:{DEFAULT_BEDROCK_MODEL_ID}"),
}

DEFAULT_EPOCHS = 3
DEFAULT_REGION = "us-east-1"
_RESULTS_PATH = "eval_specs/provider_parity_results.json"

# Error buckets. A run whose v2 arm hits either of the first two is a CONFIGURATION defect, not a
# parity verdict: the slot is INVALID and must be fixed and re-run, never written up as
# non-inferiority evidence.
SAMPLING_PARAMETER = "sampling_parameter"
AUTH_VALIDATION = "auth_validation"
GENERIC = "generic"
_INVALIDATING = (SAMPLING_PARAMETER, AUTH_VALIDATION)
_SAMPLING_PARAMS = ("temperature", "top_p", "top_k")


@dataclass(frozen=True)
class CorpusItem:
    """One gold-labelled eval case, with the ``run_case`` solver id it dispatches to."""

    spec: str
    case_id: str
    solver_id: str
    label: str  # "block" | "advisory" — the gold decision
    case: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)


@dataclass
class ArmOutcome:
    """One arm's records over the corpus, plus its error and cost accounting."""

    records: list[ItemRecord] = field(default_factory=list)
    errors: Counter[str] = field(default_factory=Counter)
    error_messages: list[str] = field(default_factory=list)
    calls: int = 0
    latency_s: float = 0.0


# ── corpus ──────────────────────────────────────────────────────────────────────────
def _spec_paths() -> list[Path]:
    from importlib.resources import files

    return sorted(Path(str(files("rebar.llm").joinpath("eval_specs"))).glob("*.eval.yaml"))


def solver_arm(solver_id: str, repo_root: str | None = None) -> str | None:
    """The ``run_case`` arm ``solver_id`` dispatches to, or ``None`` when it resolves to no
    non-agentic arm (an agentic reviewer, or an id ``run_case`` would raise on)."""
    from rebar.llm.evals import eval_solver
    from rebar.llm.plan_review import passes

    if solver_id in AGENTIC_SOLVERS:
        return None
    if solver_id == passes.PASS_NOVELTY:
        return "novelty"
    if eval_solver._criterion_id(solver_id, repo_root) is not None:
        return "criterion"
    if eval_solver._code_review_prompt_id(solver_id, repo_root=repo_root) is not None:
        return "code-review"
    return None


def eligible_cases(repo_root: str | None = None) -> list[CorpusItem]:
    """Every gold-labelled eval case whose solver id resolves to a non-agentic arm."""
    import yaml

    items: list[CorpusItem] = []
    for path in _spec_paths():
        spec = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        prompt_id = str(spec.get("prompt") or "")
        for case in spec.get("dataset") or []:
            label = GOLD_EXPECTS.get(str(case.get("expect")))
            if label is None:
                continue
            solver_id = str(case.get("criterion") or prompt_id)
            if solver_arm(solver_id, repo_root) is None:
                continue
            items.append(
                CorpusItem(
                    spec=prompt_id,
                    case_id=str(case.get("id")),
                    solver_id=solver_id,
                    label=label,
                    case=dict(case),
                )
            )
    return items


def select_corpus(items: Sequence[CorpusItem]) -> list[CorpusItem]:
    """A deterministic stratified sample: the FIRST gold-block case and the FIRST gold-advisory
    case of each spec, in that spec's dataset order, with the result ordered by (spec, case id).
    Stratifying per spec keeps the corpus balanced across criteria rather than dominated by the
    largest spec, and bounds the billable run at two cases per spec."""
    picked: dict[tuple[str, str], CorpusItem] = {}
    for item in items:
        picked.setdefault((item.spec, item.label), item)
    return sorted(picked.values(), key=lambda i: (i.spec, i.case_id))


def corpus_digest(items: Iterable[CorpusItem]) -> str:
    """A stable digest of the selected corpus, so a recorded report names the exact case set."""
    payload = [
        {"spec": i.spec, "case_id": i.case_id, "solver_id": i.solver_id, "label": i.label}
        for i in items
    ]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


# ── errors ──────────────────────────────────────────────────────────────────────────
def classify_error(text: str) -> str:
    """Bucket an epoch's exception text by ORDERED predicate. Precedence matters: a sampling
    rejection is ITSELF a ``ValidationException``, so the sampling test must run first or every
    such rejection would land in the auth/validation bucket and be mis-remediated."""
    low = text.lower()
    if any(p in low for p in _SAMPLING_PARAMS):
        return SAMPLING_PARAMETER
    if "accessdeniedexception" in low or "validationexception" in low:
        return AUTH_VALIDATION
    return GENERIC


# ── arms ────────────────────────────────────────────────────────────────────────────
def _run_case_solver(*, runner: Any = None, repo_root: str | None = None) -> Callable:
    from rebar.llm.evals.eval_solver import run_case
    from rebar.llm.runner import get_runner

    def solve(item: CorpusItem) -> dict:
        from rebar.llm.config import resolve_gate_config

        selected = runner or get_runner(resolve_gate_config(repo_root))
        return run_case(item.solver_id, item.case, runner=selected, repo_root=repo_root)

    return solve


def run_arm(
    items: Sequence[CorpusItem],
    *,
    config: LLMConfig,
    solve: Callable[[CorpusItem], dict] | None = None,
    runner: Any = None,
    epochs: int = DEFAULT_EPOCHS,
    repo_root: str | None = None,
    source: str = "local",
    concurrency: int = 1,
) -> ArmOutcome:
    """Run one arm over the corpus at ``epochs`` repeats per case, reducing to the MAJORITY
    decision. The whole loop runs inside ``gate_config(config)``, which is what makes the model
    the only variable between the arms. An epoch that raises is classified and counted, and the
    comparison continues; an item whose every epoch raised becomes an errored record.

    The loop also runs inside the gate-source read context (``source`` default ``local`` = the
    in-place checkout), because the code-review arm's prompts are tool-using: the
    raze-vet-ditch guard refuses agentic runs outside it, and every such case would otherwise be
    scored as a runtime error on BOTH arms rather than as a verdict.

    ``concurrency`` > 1 runs ITEMS in parallel — a corpus item is an independent gate op, and a
    fully sequential live run costs hours. Each worker runs under a context COPIED inside the two
    scopes above, so the arm's config and read root propagate explicitly: a bare thread would not
    inherit either ContextVar and the worker would silently resolve the AMBIENT model, which is
    precisely the split-brain that makes a parity verdict unattributable. Results are folded back
    in corpus order, so the records stay index-aligned with ``items`` regardless of scheduling."""
    import contextvars
    from concurrent.futures import ThreadPoolExecutor

    from rebar.llm import gate_source

    solve = solve or _run_case_solver(runner=runner, repo_root=repo_root)
    handle = gate_source.resolve_gate_handle(None, source, repo_root)

    def _run_item(item: CorpusItem) -> tuple[ItemRecord, list[str], int, float]:
        decisions: list[str] = []
        failures: list[str] = []
        latency = 0.0
        calls = 0
        for _ in range(max(1, epochs)):
            start = time.perf_counter()
            try:
                result = solve(item)
            except Exception as exc:  # noqa: BLE001 — an epoch failure is data, not a crash
                failures.append(f"{item.spec}/{item.case_id}: {type(exc).__name__}: {exc}")
            else:
                decisions.append(verdict_decision(result))
            finally:
                latency += time.perf_counter() - start
                calls += 1
        if decisions:
            record = ItemRecord(
                valid=True,
                decision=majority_decision(decisions),
                label=item.label,
                latency_s=latency,
            )
        else:
            record = ItemRecord(
                valid=False, decision="dropped", errored=True, label=item.label, latency_s=latency
            )
        return record, failures, calls, latency

    out = ArmOutcome()
    with gate_config(config), gate_source.gate_read_root(handle):
        if concurrency > 1 and len(items) > 1:
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                futures = [
                    pool.submit(contextvars.copy_context().run, _run_item, item) for item in items
                ]
                folded = [f.result() for f in futures]
        else:
            folded = [_run_item(item) for item in items]
    for record, failures, calls, latency in folded:
        out.records.append(record)
        out.calls += calls
        out.latency_s += latency
        for message in failures:
            out.errors[classify_error(message)] += 1
            out.error_messages.append(message)
    return out


def _drop_errored_pairs(
    v1: Sequence[ItemRecord], v2: Sequence[ItemRecord]
) -> tuple[list[ItemRecord], list[ItemRecord], int]:
    """Exclude index pairs where EITHER arm errored, keeping the two lists aligned.

    ``parity_report`` selects its gold pairs on ``a.label`` alone and then compares
    ``a.decision != b.decision`` with NO ``errored`` guard (bug 635f-783f-ae8b-430e). So no value
    of ``decision`` exempts an errored record: any gold item whose two arms disagree scores as a
    decision-level flip even when one side merely failed to run, and an ``errored=True`` record
    that still carries a plausible decision fabricates the flip silently. A single transient
    throttle on one arm would then fail the slot as a quality regression. Errors are accounted
    for in this harness's own error fields instead, where a configuration defect is remediated
    rather than mistaken for a verdict."""
    kept1: list[ItemRecord] = []
    kept2: list[ItemRecord] = []
    dropped = 0
    for a, b in zip(v1, v2, strict=True):
        if a.errored or b.errored:
            dropped += 1
            continue
        kept1.append(a)
        kept2.append(b)
    return kept1, kept2, dropped


def _serialize_records(v1: Sequence[ItemRecord], v2: Sequence[ItemRecord], items) -> list[dict]:
    rows = []
    for item, a, b in zip(items, v1, v2, strict=True):
        rows.append(
            {
                "spec": item.spec,
                "case_id": item.case_id,
                "label": item.label,
                "v1": {"valid": a.valid, "decision": a.decision, "errored": a.errored},
                "v2": {"valid": b.valid, "decision": b.decision, "errored": b.errored},
            }
        )
    return rows


def _counts(outcome: ArmOutcome) -> dict[str, int]:
    buckets = (SAMPLING_PARAMETER, AUTH_VALIDATION, GENERIC)
    return {b: int(outcome.errors.get(b, 0)) for b in buckets}


def run_slot(
    slot: str,
    items: Sequence[CorpusItem],
    *,
    v1_config: LLMConfig,
    v2_config: LLMConfig,
    epochs: int = DEFAULT_EPOCHS,
    v1_solve: Callable[[CorpusItem], dict] | None = None,
    v2_solve: Callable[[CorpusItem], dict] | None = None,
    runner: Any = None,
    repo_root: str | None = None,
    concurrency: int = 1,
) -> dict[str, Any]:
    """Run BOTH arms of one class slot over ``items`` and score them with the parity bar."""
    kw = {"runner": runner, "epochs": epochs, "repo_root": repo_root, "concurrency": concurrency}
    arm1 = run_arm(items, config=v1_config, solve=v1_solve, **kw)
    arm2 = run_arm(items, config=v2_config, solve=v2_solve, **kw)
    kept1, kept2, dropped = _drop_errored_pairs(arm1.records, arm2.records)
    report = parity.parity_report(kept1, kept2)  # DEFAULT min_gold — the floor is never lowered
    reasons = [
        f"{arm}:{bucket}={n}"
        for arm, outcome in (("v1", arm1), ("v2", arm2))
        for bucket in _INVALIDATING
        if (n := int(outcome.errors.get(bucket, 0)))
    ]
    return {
        "slot": slot,
        "measured": True,
        "v1_model": v1_config.model,
        "v2_model": v2_config.model,
        "epochs": epochs,
        "corpus_digest": corpus_digest(items),
        "n_items": len(items),
        "passed": report.passed,
        "gating_failures": report.gating_failures,
        "metrics": report.metrics,
        "records": _serialize_records(arm1.records, arm2.records, items),
        "error_counts": {"v1": _counts(arm1), "v2": _counts(arm2)},
        "error_messages": {"v1": arm1.error_messages, "v2": arm2.error_messages},
        "excluded_errored_pairs": dropped,
        "invalid": bool(reasons),
        "invalid_reasons": reasons,
        "calls": {"v1": arm1.calls, "v2": arm2.calls},
        "latency_s": {"v1": arm1.latency_s, "v2": arm2.latency_s},
    }


def unmeasured_slot(slot: str, refusal: str) -> dict[str, Any]:
    """A slot whose v2 model id could not be invoked at all. Explicitly NOT a pass: the slot
    stays an open question the first-class claim must settle."""
    return {"slot": slot, "measured": False, "refusal": refusal}


# ── the provider-clean tally ────────────────────────────────────────────────────────
def usage_model_tally(path: str | Path) -> dict[str, int]:
    """Per-call model tally from a ``REBAR_USAGE_LOG`` sink. The sink is JSONL — one
    ``json.dumps(row)`` per line with ``model`` / ``input_tokens`` KEYS — so rows are parsed as
    JSON, not grepped for ``model=``; and only rows carrying a non-null ``input_tokens`` count as
    a model call, so failure rows and verdict-JSON text cannot inflate the tally."""
    tally: Counter[str] = Counter()
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("model") and row.get("input_tokens") is not None:
            tally[str(row["model"])] += 1
    return dict(tally)


def non_bedrock_calls(tally: dict[str, int]) -> dict[str, int]:
    """The subset of a tally that did NOT resolve to Bedrock — must be empty on a v2 arm."""
    return {m: n for m, n in tally.items() if not m.startswith("bedrock:")}


# ── recorded results ────────────────────────────────────────────────────────────────
def recorded_results_path() -> Path:
    from importlib.resources import files

    return Path(str(files("rebar.llm").joinpath(_RESULTS_PATH)))


def load_recorded_results() -> dict[str, Any]:
    """Load the committed recorded live run (offline; no model)."""
    return json.loads(recorded_results_path().read_text(encoding="utf-8"))


def recheck_recorded(results: dict[str, Any]) -> dict[str, parity.ParityReport]:
    """Re-score every MEASURED slot of a recorded run against the same parity bar, with no model
    call. Raises when a slot is neither a measured report nor a quoted refusal, so a recorded run
    can never carry a slot that is silently neither."""
    reports: dict[str, parity.ParityReport] = {}
    for slot, block in (results.get("slots") or {}).items():
        if not block.get("measured"):
            if not block.get("refusal"):
                raise ValueError(f"slot {slot!r} is neither a measured report nor a refusal")
            continue
        rows = block.get("records")
        if not rows or "metrics" not in block:
            raise ValueError(f"measured slot {slot!r} carries no per-item records to re-score")
        v1 = [
            ItemRecord(
                valid=bool(r["v1"]["valid"]),
                decision=str(r["v1"]["decision"]),
                errored=bool(r["v1"]["errored"]),
                label=r["label"],
            )
            for r in rows
        ]
        v2 = [
            ItemRecord(
                valid=bool(r["v2"]["valid"]),
                decision=str(r["v2"]["decision"]),
                errored=bool(r["v2"]["errored"]),
                label=r["label"],
            )
            for r in rows
        ]
        kept1, kept2, _ = _drop_errored_pairs(v1, v2)
        reports[slot] = parity.parity_report(kept1, kept2)
    return reports


# ── the step-1 probe + the CLI ──────────────────────────────────────────────────────
def probe_slot(model_id: str, *, region: str = DEFAULT_REGION) -> dict[str, Any]:
    """Invoke one Bedrock inference-profile id twice — once bare, once with ``temperature=0``,
    the value the verifier path sends — with an ~8-token call, BEFORE any corpus spend. Each
    outcome is a completion or the exact API error string."""
    import boto3

    client = boto3.client("bedrock-runtime", region_name=region)
    messages = [{"role": "user", "content": [{"text": "Reply with the single word: ok"}]}]
    out: dict[str, Any] = {"model_id": model_id, "region": region}
    for name, cfg in (
        ("bare", {"maxTokens": 8}),
        ("temperature_0", {"maxTokens": 8, "temperature": 0}),
    ):
        try:
            resp = client.converse(modelId=model_id, messages=messages, inferenceConfig=cfg)
        except Exception as exc:  # noqa: BLE001 — the refusal string IS the probe's result
            out[name] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        else:
            out[name] = {"ok": True, "usage": resp.get("usage")}
    return out


def _slot_configs(slot: str, base: LLMConfig) -> tuple[LLMConfig, LLMConfig]:
    from dataclasses import replace

    v1_model, v2_model = CLASS_SLOTS[slot]
    return replace(base, model=v1_model), replace(base, model=v2_model)


def main(argv: list[str] | None = None) -> int:
    """Operator CLI. ``--probe`` runs step 1 only; otherwise the paired corpus run."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(prog="python -m rebar.llm.evals.provider_parity")
    parser.add_argument("--probe", action="store_true", help="run the step-1 probe and stop")
    parser.add_argument("--slot", action="append", choices=sorted(CLASS_SLOTS), default=None)
    parser.add_argument("--epochs", type=int, default=DEFAULT_EPOCHS)
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--limit", type=int, default=0, help="cap the corpus (smoke runs only)")
    parser.add_argument("--concurrency", type=int, default=1, help="parallel corpus items per arm")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true", help="print the corpus, call no model")
    args = parser.parse_args(argv)
    slots = args.slot or sorted(CLASS_SLOTS)

    if args.probe:
        probes = {
            s: probe_slot(CLASS_SLOTS[s][1].removeprefix("bedrock:"), region=args.region)
            for s in slots
        }
        sys.stdout.write(json.dumps(probes, indent=2, default=str) + "\n")
        return 0

    items = select_corpus(eligible_cases())
    if args.limit:
        items = items[: args.limit]
    if args.dry_run:
        sys.stdout.write(
            json.dumps(
                {
                    "corpus_digest": corpus_digest(items),
                    "n_items": len(items),
                    "items": [
                        {"spec": i.spec, "case_id": i.case_id, "label": i.label} for i in items
                    ],
                },
                indent=2,
            )
            + "\n"
        )
        return 0

    base = LLMConfig.from_env()
    blocks: dict[str, Any] = {}
    for slot in slots:
        v1_cfg, v2_cfg = _slot_configs(slot, base)
        blocks[slot] = run_slot(slot, items, v1_config=v1_cfg, v2_config=v2_cfg, epochs=args.epochs)
    results = {
        "schema_version": 1,
        "corpus": "provider-parity-v1",
        "recorded_at": datetime.now(UTC).isoformat(),
        "region": args.region,
        "slots": blocks,
    }
    text = json.dumps(results, indent=2, sort_keys=True) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    sys.stdout.write(text)
    measured = [b for b in blocks.values() if b.get("measured")]
    return 0 if measured and all(b["passed"] and not b["invalid"] for b in measured) else 1


if __name__ == "__main__":  # pragma: no cover — exercised by the module CLI
    raise SystemExit(main())
