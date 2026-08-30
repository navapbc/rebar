"""Tier-1: replay Pass-2 verifier questions on stored Pass-1 findings, live over the
pinned Bedrock standard-class model (Sonnet), budgeted (ticket
presolar-finable-binturong / 53ab-bdf6-de1c-4bb1).

Unlike Tier-0 (zero-LLM), Tier-1 makes REAL billable calls: for each sampled review it
re-runs the SAME Pass-2 verifier seam production uses
(:func:`rebar.llm.review_kernel.verify.verify_findings`, fed the SAME
``window_tokens``/``est_tokens`` production's plan-review wrapper supplies) against the
review's stored Pass-1 findings, under a candidate prompt/questions
(:mod:`.verifier_candidates`). Comparing the candidate's answers to the STORED answers
measures how much a prompt/question change would move Pass-2's verdicts -- the
reproduction run (``VerifierCandidate(prompt_path=None)``, production's shipped prompt)
is the commissioning run, and its agreement against stored answers IS the Pass-2 noise
floor at temperature 0.

Every call is gated by the eval budget ledger (:mod:`.ledger`) BEFORE it is made.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from rebar.llm.evals.plan_replay import corpus, ledger, parity
from rebar.llm.evals.plan_replay.sampling import stratified_sample
from rebar.llm.evals.plan_replay.tier0 import (
    _load_cache_rows,
    build_event_index,
    sidecar_data_for_row,
)
from rebar.llm.evals.plan_replay.verifier_candidates import VerifierCandidate
from rebar.llm.plan_review import det_floor, sizing
from rebar.llm.review_kernel import decide
from rebar.llm.review_kernel.verify import verify_findings

_TIER = "tier1"
_N_BUCKETS = 10  # fixed equal-width buckets for the continuous validity/priority axes


# ── sampling pool (I/O) ─────────────────────────────────────────────────────────────
def build_sampling_pool(
    store_roots: dict[str, str],
    *,
    cache_dir: Path | str,
    standard_class_model: str,
) -> list[dict[str, Any]]:
    """Corpus rows eligible for Tier-1 sampling: only ``verified`` rows whose
    ``ran_model`` matches ``standard_class_model``, enriched with the full-body
    re-read's finding count and ``impact_model_version`` for stratification (a corpus
    cache row is summary-only)."""
    manifest = corpus.build_corpus(store_roots, cache_dir=cache_dir)
    rows = _load_cache_rows(Path(cache_dir), manifest["content_hash"])
    event_index = build_event_index(store_roots)

    pool: list[dict[str, Any]] = []
    for row in rows:
        if not row.get("verified") or row.get("ran_model") != standard_class_model:
            continue
        data = sidecar_data_for_row(row, event_index, store_roots)
        if data is None:
            continue
        findings = data.get("findings")
        if not isinstance(findings, list) or not findings:
            continue
        pool.append(
            {
                **row,
                "finding_count": len(findings),
                "impact_model_version": data.get("impact_model_version"),
                "sidecar_data": data,
            }
        )
    return pool


_TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")


# ── the candidate verifier runner (live LLM seam) ───────────────────────────────────
def build_candidate_runner(
    candidate: VerifierCandidate,
) -> tuple[Callable, str, list[dict[str, Any]]]:
    """Build ``(run_chunk, model_id, usage_rows)`` for ``candidate``, pinned to the
    Bedrock standard-class model via
    :func:`rebar.llm.evals.plan_replay.parity.resolve_pinned_model` (fallback-free, so
    a Bedrock outage surfaces as a real failure, never a silent fallback to a
    different model). ``usage_rows`` is appended to IN PLACE by ``run_chunk`` on every
    call (``{model, provider, <token fields>}``), for :func:`ledger.finalize` pricing.
    When ``candidate.prompt_path`` is set, its content is installed as a
    project-override ``.rebar/prompts/plan-review-verifier.md`` in the pinned
    ephemeral config root BEFORE the prompt is resolved, so ``prompts.get_prompt``
    picks it up ahead of the packaged production prompt; ``prompt_path=None`` leaves
    the config root untouched (production's own prompt)."""
    from rebar.llm.config import LLMConfig
    from rebar.llm.plan_review import passes
    from rebar.llm.prompting import prompts
    from rebar.llm.runner import RunRequest, get_runner

    pinned = parity.resolve_pinned_model("pass2")
    if candidate.prompt_path is not None:
        override_dir = Path(pinned.config_root) / ".rebar" / "prompts"
        override_dir.mkdir(parents=True, exist_ok=True)
        (override_dir / f"{passes.PASS_VERIFIER}.md").write_text(
            Path(candidate.prompt_path).read_text(encoding="utf-8"), encoding="utf-8"
        )

    cfg = LLMConfig(model=pinned.model_id, repo_path=pinned.config_root, temperature=0.0)
    runner = get_runner(cfg)
    prompt = prompts.get_prompt(passes.PASS_VERIFIER, repo_root=cfg.repo_path)
    usage_rows: list[dict[str, Any]] = []

    def run_chunk(instructions: str, context: str) -> list[dict]:
        system, _meta = prompts.resolve_prompt(
            prompt,
            {"shared_prefix": prompts.shared_plan_prefix(context)},
            repo_root=cfg.repo_path,
        )
        req = RunRequest.for_structured(
            system_prompt=prompts.strip_volatile_marker(system),
            instructions=instructions,
            config=cfg,
            reviewers=["plan-reviewer"],
            output_schema="plan_review_verification",
            bounds=RunRequest.INHERIT_POLICY,
        )
        result = runner.run(req)
        usage = result.get("_usage") or {}
        usage_rows.append(
            {
                "model": pinned.model_id,
                "provider": "bedrock",
                **{field: int(usage.get(field, 0) or 0) for field in _TOKEN_FIELDS},
            }
        )
        return result.get("verifications", []) or []

    return run_chunk, pinned.model_id, usage_rows


# ── per-review replay ────────────────────────────────────────────────────────────────
def replay_review(row: dict[str, Any], run_chunk: Callable, model_id: str) -> dict[str, Any]:
    """Replay one sampled review's stored findings through the candidate verifier via
    the SAME production seam (:func:`verify_findings`, the SAME ``window_tokens``/
    ``est_tokens`` production supplies). A finding whose candidate answer is missing --
    degraded (its chunk's ``run_chunk`` raised) or ``omitted`` (too big to verify even
    alone) -- gets ``None`` in ``candidate_answers`` rather than an imputed value, so
    :func:`per_question_agreement` can exclude it and report the count."""
    data = row["sidecar_data"]
    findings = data["findings"]
    result = verify_findings(
        findings,
        context=row.get("description") or "",
        run_chunk=run_chunk,
        window_tokens=sizing.largest_window_tokens(model_id),
        est_tokens=det_floor.est_tokens,
    )
    candidate_verifications = result["verifications"]
    omitted = set(result["omitted"])

    stored_answers: list[dict[str, Any] | None] = []
    candidate_answers: list[dict[str, Any] | None] = []
    for i, f in enumerate(findings):
        stored_answers.append(f.get("verification"))
        candidate_answers.append(None if i in omitted else candidate_verifications.get(i))

    return {
        "ticket_id": row["ticket_id"],
        "review_event_uuid": row["review_event_uuid"],
        "stored_answers": stored_answers,
        "candidate_answers": candidate_answers,
        "model_id": model_id,
    }


# ── per-question agreement (kappa + raw agreement, no-answer excluded) ─────────────
def _cohen_kappa(pairs: list[tuple[str, str]]) -> float | None:
    """Cohen's kappa over categorical ``(stored, candidate)`` pairs. ``None`` when
    fewer than 2 pairs, or when chance agreement is total (kappa is undefined; a
    perfect observed match then reports 1.0, otherwise 0.0)."""
    if len(pairs) < 2:
        return None
    categories = sorted({v for pair in pairs for v in pair})
    n = len(pairs)
    po = sum(1 for a, b in pairs if a == b) / n
    row_marg = {c: sum(1 for a, _ in pairs if a == c) / n for c in categories}
    col_marg = {c: sum(1 for _, b in pairs if b == c) / n for c in categories}
    pe = sum(row_marg[c] * col_marg[c] for c in categories)
    if pe >= 1.0:
        return 1.0 if po >= 1.0 else 0.0
    return (po - pe) / (1 - pe)


def per_question_agreement(replayed: list[dict[str, Any]]) -> dict[str, Any]:
    """Per binary sub-question and per severity attribute: raw agreement, Cohen's
    kappa, the pair count, and a ``no_answer`` count (findings excluded because the
    candidate produced no answer -- reported, never imputed)."""
    binary_pairs: dict[str, list[tuple[str, str]]] = {}
    attr_pairs: dict[str, list[tuple[str, str]]] = {}
    no_answer: dict[str, int] = {}

    def _bump_no_answer(keys: Any) -> None:
        for k in keys:
            no_answer[k] = no_answer.get(k, 0) + 1

    for r in replayed:
        for stored, cand in zip(r["stored_answers"], r["candidate_answers"], strict=True):
            stored = stored or {}
            stored_binary = stored.get("binary", {}) or {}
            stored_attrs = stored.get("severity_attributes", {}) or {}
            if cand is None:
                _bump_no_answer(stored_binary)
                _bump_no_answer(stored_attrs)
                continue
            cand_binary = cand.get("binary", {}) or {}
            cand_attrs = cand.get("severity_attributes", {}) or {}
            for q, sv in stored_binary.items():
                cv = cand_binary.get(q)
                if cv is None:
                    _bump_no_answer([q])
                    continue
                binary_pairs.setdefault(q, []).append((sv, cv))
            for a, sv in stored_attrs.items():
                cv = cand_attrs.get(a)
                if cv is None:
                    _bump_no_answer([a])
                    continue
                attr_pairs.setdefault(a, []).append((sv, cv))

    def _summarize(pairs_by_q: dict[str, list[tuple[str, str]]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for q, pairs in pairs_by_q.items():
            raw = sum(1 for a, b in pairs if a == b) / len(pairs) if pairs else None
            out[q] = {
                "raw_agreement": raw,
                "kappa": _cohen_kappa(pairs),
                "n": len(pairs),
                "no_answer": no_answer.get(q, 0),
            }
        return out

    return {"binary": _summarize(binary_pairs), "severity_attributes": _summarize(attr_pairs)}


# ── distribution shift: fixed bins for every axis ───────────────────────────────────
def _bucket_index(value: float) -> int:
    return min(int(value * _N_BUCKETS), _N_BUCKETS - 1)


def _bucket_label(idx: int) -> str:
    lo, hi = idx / _N_BUCKETS, (idx + 1) / _N_BUCKETS
    close = "]" if idx == _N_BUCKETS - 1 else ")"
    return f"[{lo:.1f},{hi:.1f}{close}"


def _total_variation_distance(
    stored_counts: dict[str, int], candidate_counts: dict[str, int]
) -> float:
    keys = set(stored_counts) | set(candidate_counts)
    s_total = sum(stored_counts.values()) or 1
    c_total = sum(candidate_counts.values()) or 1
    return 0.5 * sum(
        abs(stored_counts.get(k, 0) / s_total - candidate_counts.get(k, 0) / c_total) for k in keys
    )


def _count_deltas(
    stored_counts: dict[str, int], candidate_counts: dict[str, int]
) -> dict[str, int]:
    keys = set(stored_counts) | set(candidate_counts)
    return {k: candidate_counts.get(k, 0) - stored_counts.get(k, 0) for k in keys}


def _continuous_axis_shift(
    stored_values: list[float], candidate_values: list[float]
) -> dict[str, Any]:
    stored_counts: dict[str, int] = {}
    candidate_counts: dict[str, int] = {}
    for v in stored_values:
        label = _bucket_label(_bucket_index(v))
        stored_counts[label] = stored_counts.get(label, 0) + 1
    for v in candidate_values:
        label = _bucket_label(_bucket_index(v))
        candidate_counts[label] = candidate_counts.get(label, 0) + 1
    return {
        "count_delta": _count_deltas(stored_counts, candidate_counts),
        "total_variation_distance": _total_variation_distance(stored_counts, candidate_counts),
    }


def _categorical_axis_shift(replayed: list[dict[str, Any]]) -> dict[str, Any]:
    """Impact's distribution shift is computed over its CATEGORICAL inputs (each
    severity-attribute/binary answer, using that question's own closed vocabulary as
    bins) rather than the derived continuous ``impact()`` scalar -- ``impact`` alone
    has no categorical vocabulary the verifier emits."""
    out: dict[str, Any] = {}
    per_question_stored: dict[str, dict[str, int]] = {}
    per_question_candidate: dict[str, dict[str, int]] = {}
    for r in replayed:
        for stored, cand in zip(r["stored_answers"], r["candidate_answers"], strict=True):
            if cand is None:
                continue
            stored = stored or {}
            for source, target in (
                (stored.get("severity_attributes", {}) or {}, per_question_stored),
                (cand.get("severity_attributes", {}) or {}, per_question_candidate),
            ):
                for q, v in source.items():
                    target.setdefault(q, {})[v] = target.setdefault(q, {}).get(v, 0) + 1
    for q in set(per_question_stored) | set(per_question_candidate):
        s = per_question_stored.get(q, {})
        c = per_question_candidate.get(q, {})
        out[q] = {
            "count_delta": _count_deltas(s, c),
            "total_variation_distance": _total_variation_distance(s, c),
        }
    return out


def distribution_shift(
    replayed: list[dict[str, Any]],
    *,
    impact_fn: Callable[[dict[str, Any]], float] = decide.impact_plan,
) -> dict[str, Any]:
    """Per-axis distribution shift (stored vs candidate), excluding findings with no
    candidate answer: ``validity``/``priority`` are continuous [0,1] scalars binned
    into the fixed ``_N_BUCKETS`` equal-width buckets; ``impact`` uses its underlying
    categorical severity-attribute/binary answers directly (see
    :func:`_categorical_axis_shift`)."""
    stored_validity: list[float] = []
    candidate_validity: list[float] = []
    stored_priority: list[float] = []
    candidate_priority: list[float] = []
    for r in replayed:
        for stored, cand in zip(r["stored_answers"], r["candidate_answers"], strict=True):
            if cand is None:
                continue
            stored = stored or {}
            s_binary = stored.get("binary", {}) or {}
            s_attrs = stored.get("severity_attributes", {}) or {}
            c_binary = cand.get("binary", {}) or {}
            c_attrs = cand.get("severity_attributes", {}) or {}
            s_val = decide.validity(s_binary)
            c_val = decide.validity(c_binary)
            stored_validity.append(s_val)
            candidate_validity.append(c_val)
            stored_priority.append(round(s_val * impact_fn(s_attrs), 4))
            candidate_priority.append(round(c_val * impact_fn(c_attrs), 4))

    return {
        "validity": _continuous_axis_shift(stored_validity, candidate_validity),
        "priority": _continuous_axis_shift(stored_priority, candidate_priority),
        "impact": _categorical_axis_shift(replayed),
    }


# ── the run driver ───────────────────────────────────────────────────────────────────
def run_tier1(
    store_roots: dict[str, str],
    *,
    cache_dir: Path | str,
    candidate: VerifierCandidate,
    candidate_name: str,
    n: int,
    seed: int,
    ledger_path: str = ledger.DEFAULT_LEDGER_PATH,
) -> dict[str, Any]:
    """The Tier-1 integration driver: samples the corpus, replays each sampled review
    through the candidate verifier (gated by the ledger pre-flight), and returns the
    aggregate report payload."""
    pinned = parity.resolve_pinned_model("pass2")
    pool = build_sampling_pool(
        store_roots, cache_dir=cache_dir, standard_class_model=pinned.model_id
    )
    sample = stratified_sample(pool, n=n, seed=seed)

    estimate_usd = ledger.estimate(_TIER, len(sample))
    ledger.reserve(estimate_usd, ledger_path=ledger_path)

    run_chunk, model_id, usage_rows = build_candidate_runner(candidate)
    replayed = [replay_review(row, run_chunk, model_id) for row in sample]

    run_id = f"{_TIER}-{candidate_name}-{uuid.uuid4().hex[:12]}"
    ledger_entry = ledger.finalize(
        run_id,
        _TIER,
        candidate_name,
        len(sample),
        {"pass2": model_id},
        usage_rows,
        ledger_path=ledger_path,
    )

    return {
        "run_id": run_id,
        "candidate": candidate_name,
        "model_id": model_id,
        "sample_n": len(sample),
        "requested_n": n,
        "seed": seed,
        "per_question_agreement": per_question_agreement(replayed),
        "distribution_shift": distribution_shift(replayed),
        "ledger_entry": ledger_entry,
    }


def render_tier1_report(result: dict[str, Any]) -> str:
    """A Markdown summary of one Tier-1 run: per-question agreement (raw + kappa +
    no-answer counts) and the distribution-shift table."""
    agreement = result["per_question_agreement"]
    lines = [
        f"# Tier-1 Pass-2 replay -- candidate `{result['candidate']}`",
        "",
        f"Run id: `{result['run_id']}`",
        f"Model: `{result['model_id']}`",
        f"Sample: {result['sample_n']} of {result['requested_n']} requested "
        f"(seed {result['seed']})",
        f"Ledger cost: ${result['ledger_entry']['usd']:.2f}",
        "",
        "## Per-question agreement (binary)",
        "",
    ]
    for q, stats in sorted(agreement["binary"].items()):
        lines.append(
            f"- `{q}`: raw={stats['raw_agreement']!r} kappa={stats['kappa']!r} "
            f"n={stats['n']} no_answer={stats['no_answer']}"
        )
    lines += ["", "## Per-attribute agreement (severity)", ""]
    for a, stats in sorted(agreement["severity_attributes"].items()):
        lines.append(
            f"- `{a}`: raw={stats['raw_agreement']!r} kappa={stats['kappa']!r} "
            f"n={stats['n']} no_answer={stats['no_answer']}"
        )
    lines += ["", "## Distribution shift", ""]
    shift = result["distribution_shift"]
    for axis in ("validity", "priority"):
        axis_result = shift[axis]
        lines.append(
            f"- `{axis}`: TVD={axis_result['total_variation_distance']:.4f} "
            f"count_delta={axis_result['count_delta']}"
        )
    lines.append("- `impact` (per underlying categorical question):")
    for q, axis_result in sorted(shift["impact"].items()):
        lines.append(
            f"  - `{q}`: TVD={axis_result['total_variation_distance']:.4f} "
            f"count_delta={axis_result['count_delta']}"
        )
    return "\n".join(lines) + "\n"
