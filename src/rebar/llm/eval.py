"""Prompt evaluation — regression detection + CI gating for git-canonical prompts
(epic a88f / WS-G).

Prompts version in our git (WS-F); this is how they evolve *safely*: a git-tracked
eval spec beside each prompt (``.rebar/evals/<id>.eval.yaml``) — dataset, thresholds,
scorers — runs through **Inspect AI** (behind the optional ``nava-rebar[eval]``
extra), and the result gates CI. The hard rule throughout: **judge
non-determinism must not create false confidence** —

  * a deterministic scorer **gates**; an LLM-judge scorer only **reports** (it never
    gates by itself);
  * no LLM-judge scorer is accepted without a **pinned grader** (temperature 0, a
    seed, a dated model snapshot, and a model family DIFFERENT from the one that
    generated the output — no self-grading);
  * gates use ``at_least(k)`` over explicit ``epochs`` (never ``pass_at``/``pass@k``),
    a coverage threshold is enforced, and judge adoption is gated by a frozen
    human-gold set + a Cohen's-kappa alignment check.

The pure logic here (spec validation, grader discipline, the gate, coverage, JUnit
conversion, kappa) is stdlib-only and offline-testable; only the actual *run*
(:func:`run_eval`) imports Inspect AI, lazily, behind ``guard_import``.

``rebar prompt eval`` reads the DIRTY working tree (the prompt as currently edited,
not the committed copy) so you iterate fast; promoting an edit still requires a
commit (the prompt is git-canonical, WS-F1).
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape as _xml_escape
from xml.sax.saxutils import quoteattr as _xml_attr

from rebar.llm.errors import LLMError

# Inspect AI floor: the version whose scorer/epoch API this seam targets.
INSPECT_MIN_VERSION = "0.3.221"

# Model families we can distinguish (for the cross-family grader rule).
_FAMILY_PREFIXES = (
    ("anthropic", ("claude", "anthropic")),
    ("openai", ("gpt", "o1", "o3", "openai")),
    ("google", ("gemini", "google")),
)


class EvalError(LLMError):
    """A prompt-eval spec is invalid, violates grader discipline, or failed to run."""


# ── spec loading ──────────────────────────────────────────────────────────────


def eval_spec_path(prompt_id: str, repo_root=None) -> Path:
    """The git-tracked eval-spec path for a prompt: ``.rebar/evals/<id>.eval.yaml``."""
    base = Path(repo_root) if repo_root else Path.cwd()
    return base / ".rebar" / "evals" / f"{prompt_id}.eval.yaml"


def _packaged_eval_spec(prompt_id: str) -> Path:
    """Packaged built-in eval spec (ships as package data), the fallback when a repo
    has no user-authored ``.rebar/evals/<id>.eval.yaml``."""
    return Path(__file__).resolve().parent / "eval_specs" / f"{prompt_id}.eval.yaml"


def load_eval_spec(prompt_id: str, *, repo_root=None) -> dict[str, Any]:
    """Load + validate a prompt's eval spec (raises :class:`EvalError` on either).

    A user ``.rebar/evals/<id>.eval.yaml`` (git-tracked beside the prompt) wins;
    otherwise a packaged built-in spec is used."""
    path = eval_spec_path(prompt_id, repo_root)
    if not path.is_file():
        path = _packaged_eval_spec(prompt_id)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise EvalError(f"no eval spec for prompt {prompt_id!r} at {path}: {exc}") from None
    import yaml

    try:
        spec = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise EvalError(f"invalid eval spec YAML for {prompt_id!r}: {exc}") from None
    errors = validate_eval_spec(spec)
    if errors:
        raise EvalError(f"eval spec for {prompt_id!r} is invalid:\n  - " + "\n  - ".join(errors))
    return spec


# ── grader discipline (WS-G2) ─────────────────────────────────────────────────


def _family(model: str | None) -> str | None:
    if not model:
        return None
    name = model.split(":", 1)[-1].lower() if ":" in model else model.lower()
    head = model.split(":", 1)[0].lower() if ":" in model else ""
    for family, prefixes in _FAMILY_PREFIXES:
        if head == family or any(name.startswith(p) for p in prefixes):
            return family
    return None


def validate_scorer(
    scorer: dict, *, generator_model: str | None = None, known: frozenset[str] | None = None
) -> list[str]:
    """Validate ONE scorer against the grader-discipline rules (WS-G2).

    A deterministic scorer gates and needs a name. When ``known`` is provided (the
    registry of implemented scorers — see :mod:`rebar.llm.eval_scorers`), the name
    must also be REGISTERED, so a typo'd or unimplemented scorer fails the offline
    gate instead of silently no-opping at run time. An ``llm-judge`` scorer MUST
    carry a pinned grader (model + temperature 0 + integer seed + dated snapshot), a
    model family different from the generator (no self-grading), an explicit
    threshold, and must NOT gate (``gates: true`` is rejected — judges report)."""
    errs: list[str] = []
    if not isinstance(scorer, dict):
        return ["scorer must be a mapping"]
    stype = scorer.get("type")
    name = scorer.get("name", "<unnamed>")
    if stype == "deterministic":
        if not scorer.get("name"):
            errs.append("deterministic scorer needs a `name`")
        elif known is not None and scorer["name"] not in known:
            errs.append(
                f"deterministic scorer {scorer['name']!r} is not a registered scorer "
                "(rebar.llm.eval_scorers.REGISTRY) — implement it or fix the name"
            )
        return errs
    if stype == "llm-judge":
        if scorer.get("gates"):
            errs.append(
                f"llm-judge scorer {name!r} must not gate (judges REPORT; set gates: false)"
            )
        if "threshold" not in scorer:
            errs.append(f"llm-judge scorer {name!r} needs an explicit `threshold`")
        grader = scorer.get("grader")
        if not isinstance(grader, dict):
            errs.append(f"llm-judge scorer {name!r} needs a pinned `grader` block")
            return errs
        if not grader.get("model"):
            errs.append(f"grader for {name!r} needs a pinned `model`")
        if grader.get("temperature", None) != 0:
            errs.append(f"grader for {name!r} must pin temperature: 0 (determinism)")
        if not isinstance(grader.get("seed"), int):
            errs.append(f"grader for {name!r} must pin an integer `seed`")
        snapshot = grader.get("snapshot")
        if not snapshot:
            errs.append(f"grader for {name!r} must pin a dated model `snapshot`")
        elif grader.get("model") and str(snapshot) not in str(grader.get("model")):
            # The snapshot must be the SAME version the pinned model id resolves to
            # — a `snapshot` that isn't embedded in the `model` id (e.g. a bare
            # `gpt-4o` paired with `snapshot: 2099-01-01`) lets the two drift, which
            # defeats the point of pinning a dated, reproducible grader.
            errs.append(
                f"grader for {name!r} snapshot {snapshot!r} is not present in the "
                f"pinned model id {grader.get('model')!r} — pin a dated model "
                "(e.g. gpt-4o-2024-08-06) whose id contains the snapshot"
            )
        gf, genf = _family(grader.get("model")), _family(generator_model)
        if gf and genf and gf == genf:
            errs.append(
                f"grader for {name!r} is the same family ({gf}) as the generator — use a "
                f"cross-family grader to avoid self-grading bias"
            )
        # length-neutral + pointwise rubric discipline (advisory but recorded).
        if scorer.get("rubric") and not scorer.get("pointwise", True):
            errs.append(
                f"llm-judge rubric {name!r} must be pointwise (per-sample), not comparative"
            )
        return errs
    return [f"scorer {name!r} has unknown type {stype!r} (deterministic | llm-judge)"]


_GATE_RE = re.compile(r"^at_least\((\d+)\)$")
_BANNED_GATES = ("pass_at", "pass@", "pass_k", "passk")


def parse_gate(gate: str) -> int:
    """Parse the gate expression — ONLY ``at_least(k)`` is allowed (WS-G2).

    ``pass_at``/``pass@k``/``pass_k`` are rejected: those report the probability a
    *single* sample passes, which is not a release gate. ``at_least(k)`` requires k
    of the explicit epochs to pass — the discipline that survives judge noise."""
    if not isinstance(gate, str):
        raise EvalError("gate must be a string of the form at_least(k)")
    low = gate.replace(" ", "").lower()
    for banned in _BANNED_GATES:
        if banned in low:
            raise EvalError(f"gate {gate!r} uses a banned pass_at/pass@k form; use at_least(k)")
    m = _GATE_RE.match(gate.replace(" ", ""))
    if not m:
        raise EvalError(f"gate {gate!r} must be at_least(k)")
    return int(m.group(1))


def validate_dataset_and_gold(spec: dict) -> list[str]:
    """STRICT dataset + gold_set checks (not enforced by the lenient
    :func:`validate_eval_spec` default, since the schema treats both as optional and
    some specs — e.g. code-quality — ship gold-only). Used by the CI discipline gate
    over the PACKAGED specs: a non-empty, balanced, well-shaped dataset and a
    non-empty gold_set. Each case needs a unique ``id``, an ``expect`` in the known
    vocabulary, and a payload (``input`` for single-doc reviewers, or ``spec`` +
    ``epics`` for the scan_spec BATCH unit). 'Balanced' = at least one should-fire
    case AND at least one good (pass) case, so the spec measures both recall and
    false-fire."""
    from rebar.llm.eval_scorers import (
        ALLOWED_EXPECTS,
        FIRE_EXPECTS,
        IMPACT_EXPECTS,
        NOFIRE_EXPECTS,
        NOVELTY_EXPECTS,
        VALIDITY_EXPECTS,
    )

    # Keys that are case METADATA, not reviewer input — a case must carry at least one
    # key OUTSIDE this set (the payload the reviewer actually consumes).
    metadata_keys = {"id", "corpus", "expect", "criterion", "kind", "pair", "mode", "note", "label"}
    errs: list[str] = []
    dataset = spec.get("dataset")
    if not isinstance(dataset, list) or not dataset:
        errs.append("strict: eval spec needs a non-empty `dataset`")
        dataset = []
    seen: set[str] = set()
    expects_used: set[str] = set()
    for i, case in enumerate(dataset):
        if not isinstance(case, dict):
            errs.append(f"strict: dataset[{i}] must be a mapping")
            continue
        cid = case.get("id")
        if not cid:
            errs.append(f"strict: dataset[{i}] needs an `id`")
        elif cid in seen:
            errs.append(f"strict: duplicate dataset id {cid!r}")
        else:
            seen.add(cid)
        expect = case.get("expect")
        if expect not in ALLOWED_EXPECTS:
            errs.append(
                f"strict: dataset[{i}] `expect`={expect!r} not in {sorted(ALLOWED_EXPECTS)}"
            )
        else:
            expects_used.add(expect)
        if not any(k not in metadata_keys and case.get(k) for k in case):
            errs.append(f"strict: dataset[{i}] needs a payload field (input/plan/finding/spec)")
    if dataset:
        if expects_used & (FIRE_EXPECTS | NOFIRE_EXPECTS) and not (
            expects_used & FIRE_EXPECTS and expects_used & NOFIRE_EXPECTS
        ):
            errs.append("strict: dataset must be balanced (>=1 should-fire AND >=1 pass case)")
        validity_axis = expects_used & VALIDITY_EXPECTS
        if validity_axis and not {"high_validity", "low_validity"} <= expects_used:
            errs.append("strict: verifier dataset needs both high_validity and low_validity")
        impact_axis = expects_used & IMPACT_EXPECTS
        if impact_axis and not {"high_impact", "low_impact"} <= expects_used:
            errs.append("strict: verifier dataset needs both high_impact and low_impact")
        novelty_axis = expects_used & NOVELTY_EXPECTS
        if novelty_axis and not {"high_novelty", "low_novelty"} <= expects_used:
            errs.append("strict: novelty dataset needs both high_novelty and low_novelty")
    gold = spec.get("gold_set")
    if not isinstance(gold, list) or not gold:
        errs.append("strict: eval spec needs a non-empty `gold_set` (judge kappa alignment)")
    else:
        for i, g in enumerate(gold):
            if not isinstance(g, dict) or not g.get("input") or not g.get("label"):
                errs.append(f"strict: gold_set[{i}] needs an `input` and a `label`")
    return errs


def validate_eval_spec(spec: dict, *, strict: bool = False) -> list[str]:
    """Validate an eval spec: explicit epochs, an at_least(k) gate, a coverage
    threshold, ≥1 scorer, at least one DETERMINISTIC (gating) scorer, and every
    scorer disciplined (WS-G2).

    ``strict=True`` additionally enforces that every deterministic scorer name is
    REGISTERED (implemented in :mod:`rebar.llm.eval_scorers`) and that the dataset +
    gold_set are present, balanced, and well-shaped. Strict mode is what the CI
    discipline gate runs over the packaged specs; the lenient default keeps
    ``load_eval_spec`` / user `.rebar/evals` specs and unit fixtures working."""
    errs: list[str] = []
    if not isinstance(spec, dict):
        return ["eval spec must be a mapping"]
    if not spec.get("prompt"):
        errs.append("eval spec needs a `prompt` id")
    epochs = spec.get("epochs")
    if not isinstance(epochs, int) or epochs < 1:
        errs.append("`epochs` must be an explicit integer >= 1")
    try:
        k = parse_gate(spec.get("gate", ""))
        # An at_least(k) gate with k > epochs can never pass — catch the
        # unsatisfiable threshold at validation time, not after a full run.
        if isinstance(epochs, int) and epochs >= 1 and k > epochs:
            errs.append(f"gate at_least({k}) is unsatisfiable: k must be <= epochs ({epochs})")
    except EvalError as exc:
        errs.append(str(exc))
    cov = spec.get("coverage_threshold")
    if not isinstance(cov, int | float) or not (0 <= cov <= 1):
        errs.append("`coverage_threshold` must be a number in [0, 1]")
    scorers = spec.get("scorers")
    if not isinstance(scorers, list) or not scorers:
        errs.append("eval spec needs a non-empty `scorers` list")
        return errs
    gen_model = spec.get("model")
    if not any(isinstance(s, dict) and s.get("type") == "deterministic" for s in scorers):
        errs.append("at least one DETERMINISTIC scorer is required to gate (judges only report)")
    known = None
    if strict:
        from rebar.llm.eval_scorers import known_scorer_names

        known = known_scorer_names()
    for s in scorers:
        errs.extend(validate_scorer(s, generator_model=gen_model, known=known))
    if strict:
        errs.extend(validate_dataset_and_gold(spec))
    return errs


# ── gate + coverage (WS-G2) ────────────────────────────────────────────────────


def at_least_passes(epoch_pass_flags: list[bool], k: int) -> bool:
    """True if at least ``k`` of the explicit epochs passed the gating scorers."""
    return sum(1 for f in epoch_pass_flags if f) >= k


def coverage(scored: int, total: int) -> float:
    """Fraction of dataset samples that produced a usable score."""
    return (scored / total) if total else 0.0


def coverage_ok(spec: dict, scored: int, total: int) -> bool:
    return coverage(scored, total) >= float(spec.get("coverage_threshold", 0))


# ── Cohen's kappa + judge alignment (WS-G3) ────────────────────────────────────


def cohens_kappa(rater_a: list, rater_b: list) -> float:
    """Cohen's kappa between two equal-length label sequences. 1.0 = perfect
    agreement, 0 = chance, negative = worse than chance. Returns 1.0 for two empty
    sequences and 1.0 when both raters are constant-and-identical (degenerate but
    perfectly aligned)."""
    if len(rater_a) != len(rater_b):
        raise EvalError("kappa needs equal-length label sequences")
    n = len(rater_a)
    if n == 0:
        return 1.0
    agree = sum(1 for a, b in zip(rater_a, rater_b, strict=True) if a == b)
    po = agree / n
    cats = set(rater_a) | set(rater_b)
    pe = sum((rater_a.count(c) / n) * (rater_b.count(c) / n) for c in cats)
    if math.isclose(pe, 1.0):
        return 1.0 if math.isclose(po, 1.0) else 0.0
    return (po - pe) / (1 - pe)


def judge_alignment(
    judge_labels: list, gold_labels: list, *, threshold: float = 0.6, judge_snapshot: str = ""
) -> dict[str, Any]:
    """Cohen's-kappa alignment between a judge and the frozen human-gold labels
    (WS-G3). ``aligned`` (kappa >= threshold) GATES whether the judge may be adopted;
    the judge's model snapshot id is recorded with the score."""
    kappa = cohens_kappa(judge_labels, gold_labels)
    return {
        "kappa": kappa,
        "threshold": threshold,
        "aligned": kappa >= threshold,
        "judge_snapshot": judge_snapshot,
        "n": len(gold_labels),
    }


# ── per-criterion calibration view (story 55b8) ───────────────────────────────
def _fired(verdict: dict | None) -> bool:
    """Fire ⇔ the criterion finder surfaced ≥1 finding (matches eval_scorers' `_fired`)."""
    return bool((verdict or {}).get("findings"))


def calibrate_criterion(
    criterion_id: str,
    *,
    repo_root=None,
    runner=None,
    runs: int = 1,
    solve=None,
) -> dict[str, Any]:
    """Run a criterion's must-fire/must-not-fire fixtures live and compute a CALIBRATION
    view (story 55b8): the same instruments rebar uses on its built-ins, so a maintainer
    decides blocking/threshold informed.

    Metrics (each over the fixture's fire/no-fire cases only — validity/impact/novelty axis
    cases are skipped):
      * ``recall``        = fired / total must-fire fixtures (`expect` ∈ {finding, fail});
      * ``false_accept``  = wrongly-fired / total must-not-fire fixtures (`expect` == pass);
      * ``agreement``     = fraction of cases where observed == expected fire/no-fire;
      * ``kappa``         = Cohen's κ of expected vs observed two-category labels;
      * ``stability``     = per-case fraction of the ``runs`` epochs agreeing with the
                            case's majority verdict (N-run stability; ``runs`` default 1).

    ``solve(prompt_id, case) -> verdict`` is injectable (mirrors :func:`run_eval`) so the
    metric math is offline-testable with no model. Raises :class:`EvalError` on an unknown
    criterion, an absent fixture (via :func:`load_eval_spec`), or an empty fire/no-fire set."""
    from rebar.llm.eval_scorers import FIRE_EXPECTS, NOFIRE_EXPECTS

    runs = max(1, int(runs))
    prompt_id = (
        criterion_id if criterion_id.startswith("plan-review-") else f"plan-review-{criterion_id}"
    )
    spec = load_eval_spec(prompt_id, repo_root=repo_root)
    fire_nofire = FIRE_EXPECTS | NOFIRE_EXPECTS
    all_cases = list(spec.get("dataset") or [])
    if not any(c.get("expect") in fire_nofire for c in all_cases):
        raise EvalError(
            f"empty calibration dataset for {criterion_id!r}: {prompt_id}.eval.yaml has no "
            "must-fire/must-not-fire cases (expect ∈ {finding, fail, pass})"
        )
    if solve is None:
        solve = _criterion_solver(repo_root=repo_root, runner=runner)

    # RUN the finder over EVERY case (must-fire / must-not-fire AND the discrimination axis
    # cases execute live), but the calibration VIEW (recall/false-accept/κ/agreement) is
    # computed only over the fire/no-fire subset — a discrimination case has no fire/no-fire
    # expectation, so it is executed + counted, never miscounted into the fire metrics.
    expected: list[str] = []
    observed: list[str] = []
    per_case: list[dict[str, Any]] = []
    n_discrimination = 0
    for case in all_cases:
        fires = [_fired(solve(prompt_id, case)) for _ in range(runs)]
        obs_fire = sum(fires) * 2 > runs  # strict majority of the N runs
        stability = sum(1 for f in fires if f == obs_fire) / runs
        if case.get("expect") not in fire_nofire:  # discrimination axis case: run, don't score
            n_discrimination += 1
            per_case.append(
                {
                    "id": case.get("id"),
                    "expect": case.get("expect"),
                    "discrimination": True,
                    "observed_fire": obs_fire,
                    "stability": stability,
                }
            )
            continue
        exp_fire = case["expect"] in FIRE_EXPECTS
        expected.append("fire" if exp_fire else "no_fire")
        observed.append("fire" if obs_fire else "no_fire")
        per_case.append(
            {
                "id": case.get("id"),
                "expect": case["expect"],
                "expected_fire": exp_fire,
                "observed_fire": obs_fire,
                "stability": stability,
            }
        )

    n_fire = expected.count("fire")
    n_nofire = expected.count("no_fire")
    tp = sum(1 for e, o in zip(expected, observed, strict=True) if e == "fire" and o == "fire")
    fp = sum(1 for e, o in zip(expected, observed, strict=True) if e == "no_fire" and o == "fire")
    stabilities = [c["stability"] for c in per_case]
    return {
        "criterion": criterion_id,
        "prompt": prompt_id,
        "runs": runs,
        "n_fire": n_fire,
        "n_nofire": n_nofire,
        "n_discrimination": n_discrimination,
        "recall": (tp / n_fire) if n_fire else None,
        "false_accept": (fp / n_nofire) if n_nofire else None,
        "agreement": sum(1 for e, o in zip(expected, observed, strict=True) if e == o)
        / len(expected),
        "kappa": cohens_kappa(expected, observed),
        "stability_min": min(stabilities),
        "stability_mean": sum(stabilities) / len(stabilities),
        "cases": per_case,
    }


def _criterion_solver(*, repo_root, runner):
    """Default calibration solver: run each case's criterion via its Pass-1 finder
    (``eval_solver.run_case`` criterion arm) with the config/live runner, threading
    ``repo_root`` so a project criterion resolves against its overlay. Missing ``agents``
    extra / credentials surface as a user-actionable :class:`EvalError` up front."""
    from rebar._optional import OptionalDependencyError
    from rebar.llm import eval_solver
    from rebar.llm.config import LLMConfig
    from rebar.llm.runner import get_runner

    try:
        cfg = LLMConfig.from_env(repo_root=repo_root)
        live = get_runner(cfg, override=runner)
        live.preflight()
    except OptionalDependencyError as exc:
        raise EvalError(str(exc)) from None

    def solve(prompt_id: str, case: dict) -> dict:
        return eval_solver.run_case(prompt_id, case, runner=live, repo_root=repo_root)

    return solve


# ── promptfoo / JUnit interop (WS-G3) ──────────────────────────────────────────


def to_junit(eval_name: str, cases: list[dict]) -> str:
    """Convert eval results to a JUnit XML suite (the promptfoo/CI interop format).

    Each case is ``{name, passed, message?, scorer?}``; a failed gating case becomes
    a ``<failure>``. CI consumes this (and promptfoo's exit-code-100 contract) to
    gate the build. The suite is wrapped in a root ``<testsuites>`` (promptfoo and
    most JUnit ingesters expect that root, not a bare ``<testsuite>``); attributes
    are emitted via ``quoteattr`` so a name/message containing a quote can't break
    the XML, and the failure message is repeated in the element body (some parsers
    read the body, not the attribute)."""
    failures = sum(1 for c in cases if not c.get("passed"))
    suite_attrs = (
        f'name={_xml_attr(eval_name)} tests="{len(cases)}" failures="{failures}" errors="0"'
    )
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f"<testsuites {suite_attrs}>",
        f"  <testsuite {suite_attrs}>",
    ]
    for c in cases:
        cname = _xml_attr(str(c.get("name", "case")))
        classname = _xml_attr(str(c.get("scorer", "eval")))
        lines.append(f"    <testcase name={cname} classname={classname}>")
        if not c.get("passed"):
            msg = str(c.get("message", "scorer failed"))
            lines.append(f"      <failure message={_xml_attr(msg)}>{_xml_escape(msg)}</failure>")
        lines.append("    </testcase>")
    lines.append("  </testsuite>")
    lines.append("</testsuites>")
    return "\n".join(lines) + "\n"


# ── the live eval run (WS-EVAL-EXISTING / 6f2d) ───────────────────────────────
# A native loop over the reviewer's REAL op (via eval_solver), reusing the
# offline-tested discipline primitives (validate / at_least(k) / coverage / to_junit /
# the eval_scorers registry). NOT routed through Inspect AI: our "model call" is a
# whole tool-using agentic op (verify_completion / review_ticket / scan_spec), which
# does not fit Inspect's single-completion solver/scorer model — wrapping it would add
# a dependency and an impedance mismatch for no gain. JUnit is emitted for promptfoo /
# CI ingestion. ``solve`` is injectable so the entire aggregation/gate/coverage/JUnit
# path is offline-testable with a fake (no model, no tokens).


def _live_solver(*, repo_root, runner):
    """Default solver: run each case's reviewer through its REAL op (eval_solver) with
    the config/live runner. Needs the ``agents`` extra (pydantic_ai); a missing one is
    a user-actionable config error (``EvalError``), not a crash."""
    from rebar._optional import OptionalDependencyError
    from rebar.llm import eval_solver
    from rebar.llm.config import LLMConfig
    from rebar.llm.runner import get_runner

    try:
        cfg = LLMConfig.from_env(repo_root=repo_root)
        live = get_runner(cfg, override=runner)
        live.preflight()  # surface a missing extra/credentials up front, before any case
    except OptionalDependencyError as exc:
        raise EvalError(str(exc)) from None

    def solve(prompt_id: str, case: dict) -> dict:
        return eval_solver.run_case(prompt_id, case, runner=live)

    return solve


def _gating_results(
    name: str, outputs: list[tuple[dict, dict | None]], epoch: int
) -> tuple[bool, list[dict]]:
    """Apply one gating scorer across an epoch's case outputs. The scorer PASSES the
    epoch iff every APPLICABLE case passes it (a scorer with no applicable cases — e.g.
    recall on a spec with no fire cases — is vacuously satisfied). Returns
    ``(passed, junit_cases)``."""
    from rebar.llm.eval_scorers import score

    failed = 0
    junit: list[dict] = []
    for case, out in outputs:
        if out is None:
            continue
        res = score(name, case, out)
        if not res.applicable:
            continue
        if not res.passed:
            failed += 1
        junit.append(
            {
                "name": f"{case.get('id', '?')}::{name}::e{epoch}",
                "scorer": name,
                "passed": res.passed,
                "message": res.detail or "",
            }
        )
    return failed == 0, junit


def run_eval(
    prompt_id: str,
    *,
    repo_root=None,
    dirty: bool = True,
    solve=None,
    runner=None,
    max_cases: int | None = None,
) -> dict[str, Any]:
    """Run a prompt's eval LIVE and return the scored result + the release gate.

    Loads + validates the git-tracked spec, then for each of ``epochs`` runs every
    dataset case through ``solve`` and applies the registered DETERMINISTIC gating
    scorers. The reviewer's prompt is resolved by its op from the DIRTY working tree
    (so a pre-commit edit is what's evaluated). The build gate is ``at_least(k)`` over
    the epochs — an epoch passes iff coverage clears the threshold AND every gating
    scorer passes. A deterministic scorer gates; llm-judge scorers only report (the
    gate never depends on a model judge), so the gate is reproducible.

    Cases run SEQUENTIALLY (concurrency 1 — no model fan-out). ``max_cases`` caps the
    number of dataset cases evaluated (a per-run cost ceiling; the CI live job sets it
    via ``REBAR_EVAL_MAX_CASES``); ``None`` runs the whole dataset.

    ``solve(prompt_id, case) -> output`` defaults to :func:`_live_solver` (the real op
    via :mod:`rebar.llm.eval_solver`, needing the ``agents`` extra); it is injectable
    so the whole aggregation path is offline-testable. Returns ``{prompt, epochs,
    gate, passed, coverage, epoch_pass, junit}``. Raises :class:`EvalError` if the spec
    is invalid or has no dataset to run."""
    spec = load_eval_spec(prompt_id, repo_root=repo_root)
    dataset = spec.get("dataset") or []
    if not dataset:
        raise EvalError(f"eval spec for {prompt_id!r} has no `dataset` to run")
    if max_cases is not None and max_cases >= 0:
        dataset = dataset[:max_cases]  # per-run cost ceiling
    epochs = int(spec["epochs"])
    k = parse_gate(spec["gate"])
    gating = [
        s["name"]
        for s in spec["scorers"]
        if isinstance(s, dict) and s.get("type") == "deterministic"
    ]
    if solve is None:
        solve = _live_solver(repo_root=repo_root, runner=runner)

    epoch_pass: list[bool] = []
    junit_cases: list[dict] = []
    coverages: list[float] = []
    for e in range(epochs):
        outputs: list[tuple[dict, dict | None]] = []
        for case in dataset:
            try:
                out: dict | None = solve(prompt_id, case)
            except Exception as exc:  # noqa: BLE001 — a failed run is an UNSCORED case, not a crash
                out = None
                junit_cases.append(
                    {
                        "name": f"{case.get('id', '?')}::run::e{e}",
                        "scorer": "run",
                        "passed": False,
                        "message": f"run error: {exc}",
                    }
                )
            outputs.append((case, out))
        scored = sum(1 for _, o in outputs if o is not None)
        coverages.append(coverage(scored, len(dataset)))
        epoch_ok = coverage_ok(spec, scored, len(dataset))
        for name in gating:
            passed, jcases = _gating_results(name, outputs, e)
            junit_cases.extend(jcases)
            epoch_ok = epoch_ok and passed
        epoch_pass.append(epoch_ok)

    return {
        "prompt": prompt_id,
        "epochs": epochs,
        "gate": spec.get("gate"),
        "passed": at_least_passes(epoch_pass, k),
        "coverage": min(coverages) if coverages else 0.0,
        "epoch_pass": epoch_pass,
        "junit": to_junit(prompt_id, junit_cases),
    }
