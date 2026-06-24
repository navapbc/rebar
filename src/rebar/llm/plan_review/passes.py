"""The three-pass review engine for the plan-review gate.

Implements the evidence → binary-verify → deterministic-gate model (adopted from
epic ``9da1``), plus the Pass-4 affirmative coach:

* **Pass 1 — find** (children ``1913``): the finder surfaces grounded FINDINGS
  ``{finding, criteria[], evidence[], scenarios[], impact}`` — NO severity, NO
  confidence. Single-turn over facet-chunks of the rubric; agentic (tool-using)
  for the code-grounding criteria.
* **Pass 2 — verify** (child ``acc1``): a SEPARATE verifier re-grounds each finding
  and emits coarse severity ATTRIBUTES + a typed BINARY sub-answer set
  ``{yes|no|insufficient}`` — one aggregate pass over all findings.
* **Pass 3 — decide** (child ``487d``): DETERMINISTIC. Computes validity (graded
  fraction of the binary answers), impact (mean of the ordinal-mapped severity
  attributes), the unified priority score (validity × impact), and the
  ``block | advisory | dropped`` decision. The model emits NO holistic
  severity/confidence anywhere in the decision path.
* **Pass 4 — coach** (child ``75a9``): a single-turn structured call over the
  SURVIVING (advisory) findings that maps each to a move from a locked registry —
  rendered deterministically (the LLM never authors free prose).

The model-driven passes (1, 2, 4) go through the shared :class:`~rebar.llm.runner.Runner`
seam, so they are fully exercisable offline with a ``FakeRunner``. Pass 3 is pure
arithmetic — no model, fully unit-testable.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from rebar.llm import contracts
from rebar.llm.config import LLMConfig
from rebar.llm.runner import Runner, RunRequest

# ── Pass-2 vocabulary (validated against criteria_v8) ──────────────────────────
GRADED_BINARY = (
    "is_verifiable",
    "evidence_entails_finding",
    "path_reachable",
    "impact_follows_necessarily",
    "no_viable_alternative_explanation",
    "no_existing_mitigation",
    "severity_claim_justified",
)
_GRADE = {"yes": 1.0, "insufficient": 0.5, "no": 0.0}
_SEV01 = {"none": 0.0, "low": 0.33, "medium": 0.67, "high": 1.0}
_BLAST01 = {"local": 0.33, "module": 0.67, "system": 1.0}
_LIKE01 = {"low": 0.33, "medium": 0.67, "high": 1.0}
_REV01 = {"easy": 0.33, "moderate": 0.67, "hard": 1.0}

DEFAULT_BLOCK_THRESHOLD = 0.95  # near-certain AND high-impact ⇒ v1 is almost all advisory


# ── structured-output contracts (registered once on import) ────────────────────
def _pass1_model() -> type:
    from pydantic import BaseModel, Field

    class P1Finding(BaseModel):
        finding: str = Field(description="The defect/gap, stated as a claim to verify.")
        criteria: list[str] = Field(default_factory=list, description="Rubric criterion id(s).")
        evidence: list[str] = Field(
            default_factory=list,
            description="Flexible grounding: a plan quote, section name, or ABSENCE rationale.",
        )
        scenarios: list[str] = Field(default_factory=list, description="Where this bites.")
        impact: str = Field(default="", description="Consequence if unaddressed.")

    class P1Output(BaseModel):
        analysis: str = Field(default="", description="Scratchpad — reason before emitting.")
        findings: list[P1Finding] = Field(default_factory=list)

    return P1Output


def _pass2_model() -> type:
    from pydantic import BaseModel, Field

    class SeverityAttrs(BaseModel):
        prod_impact: str = Field(default="none", description="none|low|medium|high")
        debt_impact: str = Field(default="none", description="none|low|medium|high")
        blast_radius: str = Field(default="local", description="local|module|system")
        likelihood: str = Field(default="low", description="low|medium|high")
        reversibility: str = Field(default="easy", description="easy|moderate|hard")

    class Binary(BaseModel):
        cited_reference_accurate: str = Field(default="na", description="yes|no|insufficient|na")
        is_verifiable: str = Field(default="insufficient")
        evidence_entails_finding: str = Field(default="insufficient")
        path_reachable: str = Field(default="insufficient")
        impact_follows_necessarily: str = Field(default="insufficient")
        no_viable_alternative_explanation: str = Field(default="insufficient")
        no_existing_mitigation: str = Field(default="insufficient")
        severity_claim_justified: str = Field(default="insufficient")

    class Verification(BaseModel):
        index: int = Field(description="The 0-based index of the finding being verified.")
        severity_attributes: SeverityAttrs = Field(default_factory=SeverityAttrs)
        binary: Binary = Field(default_factory=Binary)

    class P2Output(BaseModel):
        verifications: list[Verification] = Field(default_factory=list)

    return P2Output


def _pass4_model() -> type:
    from pydantic import BaseModel, Field

    class CoachNote(BaseModel):
        move_id: str = Field(description="A move id from the locked move registry.")
        subject: str = Field(
            description="A short noun-phrase subject (≤8 words; no code, no imperative)."
        )
        finding_refs: list[str] = Field(
            default_factory=list, description="The finding id(s) this move addresses."
        )

    class P4Output(BaseModel):
        notes: list[CoachNote] = Field(default_factory=list)

    return P4Output


def register_contracts() -> None:
    """Register the per-pass structured-output contracts (idempotent)."""
    contracts.register_contract("plan_review_findings", _pass1_model)
    contracts.register_contract("plan_review_verification", _pass2_model)
    contracts.register_contract("plan_review_coach", _pass4_model)


register_contracts()


# ── prompts ────────────────────────────────────────────────────────────────────
_DECISIVENESS = (
    "\n\nDECISIVENESS: Reserve an absence/AMBIGUOUS finding for cases where the PLAN ITSELF "
    "genuinely under-specifies the criterion, or where a specific codebase fact is load-bearing "
    "AND unknowable from the plan text. Do NOT raise a finding merely because you cannot run or "
    "read the live code: if the plan's own text affirmatively satisfies the criterion, it PASSES "
    "(emit no finding). A well-specified plan you simply can't execute is a PASS."
)

PASS1_SYSTEM = (
    "You are an expert software-plan reviewer running PASS 1 of a three-pass review. Your job is "
    "to COACH the author toward a better plan by surfacing grounded findings — not to nitpick or "
    "roadblock. Output one record PER distinct finding: {finding, criteria[], evidence[], "
    "scenarios[], impact}. criteria[] = the rubric id(s) the finding maps to. evidence[] = "
    "flexible free text: a quoted plan phrase, a named section, an ABSENCE rationale (plan-review "
    "findings are often non-citable), or a code citation. Do NOT emit severity, confidence, or "
    "priority — a separate pass computes those. Ground every finding in specific evidence. A clean "
    "chunk returns an empty findings list (that is expected and good)." + _DECISIVENESS
)

PASS2_SYSTEM = (
    "You are an INDEPENDENT verifier running PASS 2 of a three-pass review. Each finding below is "
    "an unproven CLAIM TO TEST — its conclusion is NOT asserted; do not assume it is correct. "
    "Re-ground in the plan (and, for code-grounded findings, the actual code). For EACH finding, "
    "by its 0-based index, emit (a) coarse severity ATTRIBUTES {prod_impact, debt_impact "
    "(none|low|medium|high), blast_radius (local|module|system), likelihood (low|medium|high), "
    "reversibility (easy|moderate|hard)} and (b) typed BINARY sub-answers (yes|no|insufficient). "
    "cited_reference_accurate is yes|no|insufficient|na — answer it only when the finding cites a "
    "specific code reference, else na). Be atomic: answer each sub-question on its own merits. "
    "'insufficient' is allowed and honest. Verdict-with-citation, never verdict-with-fix."
)

PASS4_SYSTEM = (
    "You are an affirmative COACH running PASS 4 (advisory; after the gate decision). For each "
    "SURVIVING advisory finding, select the single most useful MOVE from the move registry below "
    "and extract a short noun-phrase SUBJECT (≤8 words). Output {move_id, subject, finding_refs}. "
    "Reference findings BY their id — never restate them. The subject must be a NOUN PHRASE naming "
    "what to investigate (e.g. 'the retry/timeout policy'), NOT code, NOT an imperative, NOT the "
    "solution. The coaching prose is rendered deterministically from the move template — you only "
    "pick the move and name the subject."
)


# ── helpers ─────────────────────────────────────────────────────────────────────
def _criterion_block(c: dict[str, Any]) -> str:
    checks = c.get("checklist") or []
    bullets = "\n".join(f"    - {ck.get('check', ck)}" for ck in checks) if checks else ""
    body = f"[{c['id']}] {c.get('name', '')}\n  {c.get('scenario', '')}"
    return body + (f"\n  Checklist:\n{bullets}" if bullets else "")


def _plan_system(system: str, ctx_plan: str) -> str:
    """System prompt with the WHOLE plan appended (never chunked/truncated)."""
    return f"{system}\n\n# Plan under review (verbatim, whole)\n{ctx_plan}"


# ── Pass 1: find ─────────────────────────────────────────────────────────────────
def pass1_chunk(
    runner: Runner,
    cfg: LLMConfig,
    *,
    plan: str,
    chunk: list[dict[str, Any]],
    agentic: bool = False,
) -> list[dict[str, Any]]:
    """Run one Pass-1 finder call over a chunk of criteria. Returns the findings
    (each tagged with the criteria it maps to). Single-turn unless ``agentic``
    (the code-grounding tier)."""
    ids = [c["id"] for c in chunk]
    rubric = "\n\n".join(_criterion_block(c) for c in chunk)
    req = RunRequest(
        system_prompt=_plan_system(PASS1_SYSTEM, plan),
        instructions=(
            f"## Rubric criteria for this pass (ids: {', '.join(ids)})\n{rubric}\n\n"
            "Surface every grounded finding for these criteria. Return ONLY findings whose "
            "`criteria` are in this id set; an empty list for a clean chunk is correct."
        ),
        config=cfg,
        reviewers=["plan-reviewer"],
        mode="structured",
        output_schema="plan_review_findings",
        execution_mode="agentic" if agentic else "single_turn",
    )
    result = runner.run(req)
    out: list[dict[str, Any]] = []
    for f in result.get("findings", []) or []:
        crit = [c for c in (f.get("criteria") or []) if c in ids] or ids[:1]
        out.append(
            {
                "finding": f.get("finding", ""),
                "criteria": crit,
                "evidence": f.get("evidence", []) or [],
                "scenarios": f.get("scenarios", []) or [],
                "impact": f.get("impact", ""),
                "_agentic": agentic,
            }
        )
    return out


# ── Pass 2: verify ───────────────────────────────────────────────────────────────
def pass2_verify(
    runner: Runner,
    cfg: LLMConfig,
    *,
    plan: str,
    findings: list[dict[str, Any]],
    agentic: bool = False,
    batch_size: int = 12,
) -> dict[int, dict[str, Any]]:
    """One aggregate verification pass over ALL findings (batched, NOT per-finding).
    Returns ``{finding_index: {severity_attributes, binary}}``. Agentic (tool-using)
    when any code-grounded finding is present; single-turn otherwise."""
    if not findings:
        return {}
    out: dict[int, dict[str, Any]] = {}
    for start in range(0, len(findings), batch_size):
        batch = list(enumerate(findings))[start : start + batch_size]
        listing = "\n\n".join(
            f"### finding index {i}\nclaim: {f['finding']}\ncriteria: {', '.join(f['criteria'])}\n"
            f"evidence: {' | '.join(f.get('evidence', []))}\nimpact: {f.get('impact', '')}"
            for i, f in batch
        )
        req = RunRequest(
            system_prompt=_plan_system(PASS2_SYSTEM, plan),
            instructions=(
                "Verify each finding below by its index. Emit one verification per finding "
                f"(indices {batch[0][0]}–{batch[-1][0]}).\n\n{listing}"
            ),
            config=cfg,
            reviewers=["plan-verifier"],
            mode="structured",
            output_schema="plan_review_verification",
            execution_mode="agentic" if agentic else "single_turn",
        )
        result = runner.run(req)
        for v in result.get("verifications", []) or []:
            idx = v.get("index")
            if isinstance(idx, int):
                out[idx] = {
                    "severity_attributes": v.get("severity_attributes", {}) or {},
                    "binary": v.get("binary", {}) or {},
                }
    return out


# ── Pass 3: decide (DETERMINISTIC — no model in this path) ────────────────────────
def validity(binary: dict[str, Any]) -> float:
    """The graded fraction of the binary sub-answers (yes=1, insufficient=.5,
    no=0) over the answerable graded set (excluding any 'na'). The cited-reference
    veto is handled separately. Empty ⇒ 0.0."""
    scores = [
        _GRADE[binary[q]] for q in GRADED_BINARY if binary.get(q) in ("yes", "no", "insufficient")
    ]
    return round(sum(scores) / len(scores), 4) if scores else 0.0


def impact(attrs: dict[str, Any]) -> float:
    """IMPACT ∈ [0,1] = mean of the ordinal-mapped severity attributes:
    max(prod_impact, debt_impact), blast_radius, likelihood, reversibility."""
    sev = max(_SEV01.get(attrs.get("prod_impact"), 0.0), _SEV01.get(attrs.get("debt_impact"), 0.0))
    blast = _BLAST01.get(attrs.get("blast_radius"), 0.33)
    like = _LIKE01.get(attrs.get("likelihood"), 0.33)
    rev = _REV01.get(attrs.get("reversibility"), 0.33)
    return round((sev + blast + like + rev) / 4.0, 4)


def severity_label(imp: float) -> str:
    if imp >= 0.75:
        return "critical"
    if imp >= 0.5:
        return "major"
    if imp >= 0.25:
        return "minor"
    return "none"


def pass3_decide(
    verification: dict[str, Any] | None,
    *,
    block_threshold: float = DEFAULT_BLOCK_THRESHOLD,
    blocking_enabled: bool = False,
) -> dict[str, Any]:
    """The deterministic decision. Returns
    ``{decision, reason, validity, impact, priority, severity}``.

    Rules (the v1 authoritative shape):
      * no verification → INDETERMINATE (verifier produced nothing for this finding);
      * cited_reference_accurate == "no" → DROPPED (the only veto, fires only when a
        code citation is present);
      * validity < 0.5 → DROPPED (low validity);
      * else BLOCK iff (not vetoed) AND blocking_enabled AND priority ≥ block_threshold;
      * else ADVISORY.
    """
    if not verification:
        return {
            "decision": "indeterminate",
            "reason": "no-verification",
            "validity": 0.0,
            "impact": 0.0,
            "priority": 0.0,
            "severity": "none",
        }
    binary = verification.get("binary", {}) or {}
    attrs = verification.get("severity_attributes", {}) or {}
    val = validity(binary)
    imp = impact(attrs)
    priority = round(val * imp, 4)
    sev = severity_label(imp)
    if binary.get("cited_reference_accurate") == "no":
        return {
            "decision": "dropped",
            "reason": "veto:cited-reference-inaccurate",
            "validity": val,
            "impact": imp,
            "priority": priority,
            "severity": sev,
        }
    if val < 0.5:
        decision, reason = "dropped", "low-validity"
    elif blocking_enabled and priority >= block_threshold:
        decision, reason = "block", "high-priority+criterion-opted-in"
    else:
        decision, reason = "advisory", "default-advisory"
    return {
        "decision": decision,
        "reason": reason,
        "validity": val,
        "impact": imp,
        "priority": priority,
        "severity": sev,
    }


# ── Pass 4: coach (rendered deterministically from a locked template) ─────────────
def pass4_coach(
    runner: Runner,
    cfg: LLMConfig,
    *,
    plan: str,
    surviving: list[dict[str, Any]],
    move_registry: dict[str, dict[str, str]],
) -> list[dict[str, Any]]:
    """Map each surviving advisory finding to a move and render coaching prose
    DETERMINISTICALLY from the move's locked template. The LLM only picks the move
    and names a bounded noun-phrase subject (validated); it never authors prose."""
    if not surviving:
        return []
    listing = "\n".join(f"- id={f['id']} :: {f['finding'][:200]}" for f in surviving)
    moves = "\n".join(f"  {mid}: {m['name']}" for mid, m in sorted(move_registry.items()))
    req = RunRequest(
        system_prompt=_plan_system(PASS4_SYSTEM, plan),
        instructions=(
            f"## Move registry\n{moves}\n\n## Surviving advisory findings (by id)\n{listing}\n\n"
            "Emit one note per finding you can map to a useful move (skip findings no move fits)."
        ),
        config=cfg,
        reviewers=["plan-coach"],
        mode="structured",
        output_schema="plan_review_coach",
        execution_mode="single_turn",
    )
    result = runner.run(req)
    notes: list[dict[str, Any]] = []
    for n in result.get("notes", []) or []:
        move = move_registry.get(n.get("move_id", ""))
        subject = _validate_subject(n.get("subject", ""))
        if not move or subject is None:
            continue  # C1 fallback: no valid subject ⇒ emit no coaching for it
        notes.append(
            {
                "move_id": n["move_id"],
                "move_name": move["name"],
                "subject": subject,
                "finding_refs": n.get("finding_refs", []) or [],
                "coaching": move["template"].format(subject=subject),
            }
        )
    return notes


_IMPERATIVE_STARTS = (
    "add",
    "remove",
    "use",
    "create",
    "run",
    "implement",
    "write",
    "fix",
    "change",
    "delete",
    "refactor",
    "call",
    "set",
    "make",
    "update",
    "replace",
)


def _validate_subject(subject: str) -> str | None:
    """The SUBJECT VALIDATOR (the load-bearing C1 enforcement): a bounded
    noun-phrase — ≤8 words / ≤60 chars, no code tokens, not a leading imperative.
    Returns the cleaned subject or None (reject → no coaching for that finding)."""
    s = (subject or "").strip()
    if not s or len(s) > 60 or len(s.split()) > 8:
        return None
    if any(tok in s for tok in ("(", ")", "{", "}", ";", "=", "`", "()", "import ")):
        return None
    if s.split()[0].lower().rstrip(":,.") in _IMPERATIVE_STARTS:
        return None
    return s


def verifier_cfg(cfg: LLMConfig) -> LLMConfig:
    """The Pass-2 verifier uses a decisive non-frontier model (Sonnet) unless the
    operator explicitly chose a model — mirrors the completion-verifier default."""
    from rebar.llm.config import DEFAULT_MODEL

    if cfg.model == DEFAULT_MODEL:
        return replace(cfg, model="claude-sonnet-4-6")
    return cfg
