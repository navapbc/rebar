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


def validate_scorer(scorer: dict, *, generator_model: str | None = None) -> list[str]:
    """Validate ONE scorer against the grader-discipline rules (WS-G2).

    A deterministic scorer gates and needs only a name. An ``llm-judge`` scorer
    MUST carry a pinned grader (model + temperature 0 + integer seed + dated
    snapshot), a model family different from the generator (no self-grading), an
    explicit threshold, and must NOT gate (``gates: true`` is rejected — judges
    report)."""
    errs: list[str] = []
    if not isinstance(scorer, dict):
        return ["scorer must be a mapping"]
    stype = scorer.get("type")
    name = scorer.get("name", "<unnamed>")
    if stype == "deterministic":
        if not scorer.get("name"):
            errs.append("deterministic scorer needs a `name`")
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


def validate_eval_spec(spec: dict) -> list[str]:
    """Validate an eval spec: explicit epochs, an at_least(k) gate, a coverage
    threshold, ≥1 scorer, at least one DETERMINISTIC (gating) scorer, and every
    scorer disciplined (WS-G2)."""
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
            errs.append(
                f"gate at_least({k}) is unsatisfiable: k must be <= epochs ({epochs})"
            )
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
    for s in scorers:
        errs.extend(validate_scorer(s, generator_model=gen_model))
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
        f"name={_xml_attr(eval_name)} tests=\"{len(cases)}\" "
        f'failures="{failures}" errors="0"'
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


# ── the Inspect AI run seam (WS-G1) ───────────────────────────────────────────


def run_eval(prompt_id: str, *, repo_root=None, dirty: bool = True) -> dict[str, Any]:
    """Run a prompt's eval through Inspect AI (needs the ``eval`` extra).

    Loads + validates the git-tracked spec, then evaluates the prompt — reading the
    DIRTY working-tree prompt text (so you iterate before committing). Returns a
    result dict ``{prompt, epochs, gate, passed, coverage, scores[], junit}``.

    Two distinct failure modes, deliberately different exception types:
      * ``EvalError`` — the spec is invalid, or the ``eval`` extra (``inspect_ai``)
        is not installed. Both are user-actionable config errors.
      * ``NotImplementedError`` — the extra IS present but the concrete live-model
        Inspect harness is not wired into this build. This is NOT a config error,
        so it must not masquerade as one; the offline-testable discipline
        (validate_eval_spec/at_least_passes/coverage/cohens_kappa/to_junit) is what
        gates locally, and the live run is exercised by the eval CI."""
    from rebar._optional import OptionalDependencyError, guard_import

    spec = load_eval_spec(prompt_id, repo_root=repo_root)
    try:
        guard_import("inspect_ai", extra="eval")  # the heavy import lives behind the extra
    except OptionalDependencyError as exc:
        raise EvalError(str(exc)) from None
    _ = spec  # the Inspect Task is assembled from this in the live harness
    # The concrete Inspect Task wiring (dataset → solver(prompt) → scorers → epochs)
    # is assembled here from `spec`; kept thin so the disciplined pieces above
    # (grader checks, at_least(k), coverage, kappa) are what the tests pin. The live
    # model run is exercised by the external/eval CI with credentials, not offline.
    raise NotImplementedError(
        "run_eval's live-model Inspect harness is not wired into this build; the "
        "offline-testable discipline (validate_eval_spec/validate_scorer/"
        "at_least_passes/coverage/cohens_kappa/to_junit) gates locally. Run the "
        "full evaluation via the eval CI workflow (needs credentials)."
    )
