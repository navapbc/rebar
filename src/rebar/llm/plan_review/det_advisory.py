"""The NEVER-BLOCKING half of the plan-review DET floor: P2, P3, P6, P7.

Extracted from :mod:`.det_floor` (which owns :class:`.det_floor.DetResult`,
:class:`.det_floor.PlanContext`, the ``DET_CHECKS`` registry, ``run_det_floor`` and the
finding/coverage aggregation) along the boundary that module's own docstring already
draws: "the only sound, unambiguous blockers are P1, P4 (description), P5 (cycle), P8,
P10 and P11 — everything else is advisory or coverage-only". This module is that
everything else.

The boundary is a real seam in the call graph, not a line-count carve. None of the four
checks here calls — or is called by — anything that stayed in ``det_floor``:

* **P2 resolution** and **P3 package existence** are COVERAGE-ONLY oracle probes. Each
  extracts references with its own private pattern (:data:`_FILE_REF_RE` /
  :data:`_PKG_REF_RE`) and probes them through
  :func:`rebar.grounding.refute_absence`, lazily imported so this module carries no
  import-time dependency on the grounding stack. Neither ever blocks: a plan may
  legitimately reference a file it will create or a dependency it will add, so a
  non-resolving reference is recorded as coverage, never as a defect. Fail-open — any
  oracle or extraction error becomes an ``abstain``.
* **P6 AC/DD quality** is the ADVISORY aggregation hub over the lint detectors already
  extracted into siblings: :func:`.det_operator_attested.ac_item_lines` and
  ``operator_evidence_issues``, :func:`.det_clarity.vague_hits_in_line`,
  :func:`.det_lint._verify_command_strings` / ``_lint_verify_command``,
  :func:`.det_measurement_provenance.provenance_issues` and
  :func:`.det_citation.unbacked_citations`. Moving it puts the hub beside its spokes.
* **P7 destructive sniff** owns :data:`_DESTRUCTIVE_RE` / :data:`_SAFEGUARD_RE` and calls
  nothing at all.

The retained half is, by contrast, connected: P1 and P4 share ``_count_ac_items``, P1
adds ``_clarity_score``, P4 adds ``_description_limit``, P8 uses ``est_tokens``, and P5
uses ``det_lint``'s graph helpers. Cutting between the two sets severs no call edge.

Cycle-freedom follows the contract :mod:`.det_lint` and :mod:`.det_clarity` established:
``from __future__ import annotations`` plus ``TYPE_CHECKING`` for the ``DetResult`` /
``PlanContext`` annotations, and a lazy ``from .det_floor import DetResult`` inside each
body — so ``det_floor``'s module-level import of these checks (which it re-exports, to
keep every existing import point and attribute access unchanged) never closes a loop.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from .det_clarity import vague_hits_in_line
from .det_lint import _lint_verify_command, _verify_command_strings

if TYPE_CHECKING:
    from .det_floor import DetResult, PlanContext


# ── P2 file/symbol/import resolution (oracle, fail-open, coverage-only) ─────────
# A backticked token that looks like a repo file path: has a slash and a dotted
# extension, no spaces. Conservative on purpose (low false-extraction).
_FILE_REF_RE = re.compile(r"`([\w./\-]+/[\w.\-]+\.[A-Za-z0-9]+)`")


def p2_resolution(ctx: PlanContext) -> DetResult:
    """Coverage-only. Probe explicit file-path references in the plan against the
    grounding oracle (universal-ctags T1). NEVER blocks: a plan referencing a file
    it will *create* is legitimate, so a non-resolving reference is not a defect —
    only the coverage (how many references resolved) is recorded. Fail-open: any
    oracle/extraction error → abstain."""
    from .det_floor import DetResult  # lazy: det_floor imports this module at load

    if not ctx.repo_root:
        return DetResult(
            "P2", "resolution", "abstain", coverage={"ran": False, "reason": "no_repo_root"}
        )
    refs = sorted(set(_FILE_REF_RE.findall(ctx.plan_text)))
    if not refs:
        return DetResult(
            "P2", "resolution", "pass", coverage={"ran": True, "references": 0, "resolved": 0}
        )
    try:
        from rebar import grounding
    except Exception as exc:  # noqa: BLE001 — grounding oracle is optional; any import failure ⇒ fail-open abstain (reason recorded)
        return DetResult(
            "P2", "resolution", "abstain", coverage={"ran": False, "reason": f"oracle:{exc}"}
        )
    resolved = abstained = 0
    for ref in refs[:50]:  # bound the probe; coverage records the cap
        try:
            ev = grounding.refute_absence({"kind": "file", "name": ref}, repo_root=ctx.repo_root)
            if ev.get("outcome") == "refuted":  # refuting absence == it exists
                resolved += 1
            else:
                abstained += 1
        except Exception:  # noqa: BLE001 — per-reference best-effort probe; an unprobeable ref abstains, never blocks
            abstained += 1
    return DetResult(
        "P2",
        "resolution",
        "pass",
        coverage={
            "ran": True,
            "references": len(refs),
            "resolved": resolved,
            "unresolved_or_abstained": abstained,
            "probed": min(len(refs), 50),
        },
    )


# ── P3 package existence (oracle T0, fail-open, coverage-only) ──────────────────
_PKG_REF_RE = re.compile(
    r"(?:pip install|npm install|cargo add|go get|gem install|add dependency)\s+([\w.\-]+)",
    re.IGNORECASE,
)


def p3_package_existence(ctx: PlanContext) -> DetResult:
    """Coverage-only. Probe explicit dependency references against the oracle's T0
    deps lane (deps.dev registry + optional syft). NEVER blocks (a plan may add a
    brand-new dep). Fail-open: any error → abstain."""
    from .det_floor import DetResult  # lazy: det_floor imports this module at load

    pkgs = sorted(set(_PKG_REF_RE.findall(ctx.plan_text)))
    if not pkgs:
        return DetResult("P3", "package-existence", "pass", coverage={"ran": True, "packages": 0})
    try:
        from rebar import grounding
    except Exception as exc:  # noqa: BLE001 — grounding oracle is optional; any import failure ⇒ fail-open abstain (reason recorded)
        return DetResult(
            "P3",
            "package-existence",
            "abstain",
            coverage={"ran": False, "reason": f"oracle:{exc}"},
        )
    existing = abstained = 0
    for pkg in pkgs[:25]:
        try:
            ev = grounding.refute_absence(
                {"kind": "dependency", "name": pkg}, repo_root=ctx.repo_root or "."
            )
            if ev.get("outcome") == "refuted":
                existing += 1
            else:
                abstained += 1
        except Exception:  # noqa: BLE001 — per-package best-effort probe; an unprobeable dep abstains, never blocks
            abstained += 1
    return DetResult(
        "P3",
        "package-existence",
        "pass",
        coverage={"ran": True, "packages": len(pkgs), "existing": existing, "abstained": abstained},
    )


# ── P6 AC/DD quality (lexical, advisory) ───────────────────────────────────────
def p6_ac_quality(ctx: PlanContext) -> DetResult:
    """Advisory. Lexical AC quality checks: compound-AND criteria (one item
    bundling multiple deliverables joined by ' and '), vague/subjective lexicon,
    and whether any verification command/section is present. Never blocks."""
    from . import det_citation, det_measurement_provenance, det_operator_attested
    from .det_floor import DetResult  # lazy: det_floor imports this module at load

    items = det_operator_attested.ac_item_lines(ctx.plan_text)
    issues: list[str] = []
    compound = [
        it
        for it in items
        if re.search(r"\band\b", it, re.IGNORECASE)
        and (it.count(",") + len(re.findall(r"\band\b", it, re.IGNORECASE))) >= 2
    ]
    if compound:
        issues.append(
            f"{len(compound)} criterion line(s) bundle multiple deliverables with 'and' "
            "(split so each is independently verifiable)."
        )
    low = ctx.plan_text.lower()
    # Same FIXED matching as the blocking P11 (det_clarity: both word boundaries,
    # `clean` dropped, code-span aware) so the two surfaces agree — but P6 stays
    # advisory and scans the WHOLE plan text, not just AC items.
    vague_hits = sorted({t for ln in ctx.plan_text.split("\n") for t in vague_hits_in_line(ln)})
    if vague_hits:
        issues.append(f"vague/subjective terms present: {', '.join(vague_hits[:8])}")
    has_verify = bool(ctx.state.get("verify_commands")) or "verif" in low or "test" in low
    if not has_verify:
        issues.append("no verification commands or testing plan referenced")
    # Verify-command lint (G-3a, WS4): mechanically-checkable defects in the stated proving
    # commands. Per-line abstains AGGREGATE into the single P6 coverage dict as counts (never
    # per-line events) so the DET floor stays P1-P9 (this extends p6, adds no check).
    linted = _verify_command_strings(ctx)
    lint_abstained = 0
    for cmd in linted:
        defect, abstained = _lint_verify_command(cmd)
        if abstained:
            lint_abstained += 1
        elif defect:
            issues.append(defect)
    # Operator-attested evidence-kind lint (R2, ADR-0043): AC items whose "done" evidence lives
    # OUTSIDE the codebase but are not tagged [operator-attested]. ADVISORY coaching only (p6
    # never blocks); each gap's fix is inline. Detector extracted to det_operator_attested and
    # self-gated by the deterministic lexicon eval (docs/experiments/plan-review-gate/).
    oa_issues = det_operator_attested.operator_evidence_issues(items)
    issues.extend(oa_issues)
    # Measurement-provenance lint (story f161, ADR-0043 x ADR-0016): [operator-attested] AC
    # items whose measurement-provenance continuation line is absent/incomplete/placeholder/
    # enum-invalid. ADVISORY coaching only (p6 never blocks); each gap's fix is inline. Needs
    # the RAW plan text (not just checkbox lines) to see the indented continuation line.
    prov_issues = det_measurement_provenance.provenance_issues(ctx.plan_text)
    issues.extend(prov_issues)
    # Cross-ticket citation edge-verify lint (story 266e): a `[rebar:<id>]` citation whose
    # cited id is not a VERIFIED upstream prerequisite of P (P.depends_on(C) or C.blocks(P)).
    # ADVISORY coaching only (p6 never blocks); Layer-2 (the LLM finders) owns crediting.
    # Reverse lookup is fail-closed inside det_citation. Logic lives in det_citation (det_floor
    # is size-ceilinged); this is just the call + extend.
    from rebar import _reads

    def _resolve_deps(cid: str) -> list[dict[str, Any]]:
        return _reads.show_ticket(cid, repo_root=ctx.tickets_root).get("deps", []) or []

    # Inherited-link resolution (client report §7): a child's citation is grounded when an
    # ANCESTOR carries the upstream edge (epics depend on epics, stories on stories).
    def _resolve_parent(cid: str) -> str | None:
        return _reads.show_ticket(cid, repo_root=ctx.tickets_root).get("parent_id") or None

    cit_issues = det_citation.unbacked_citations(
        det_citation.parse_citations(ctx.plan_text),
        ctx.state.get("deps", []) or [],
        _resolve_deps,
        ctx.ticket_id,
        _resolve_parent,
    )
    issues.extend(cit_issues)
    cov = {
        "ran": True,
        "ac_items": len(items),
        "verify_commands_linted": len(linted),
        "verify_lint_abstained": lint_abstained,
        "operator_attested_gaps": len(oa_issues),
        "provenance_gaps": len(prov_issues),
        "citation_gaps": len(cit_issues),
    }
    if not issues:
        return DetResult("P6", "ac-quality", "pass", coverage=cov)
    return DetResult(
        "P6",
        "ac-quality",
        "fail",
        finding={
            "finding": "Acceptance-criteria / definition-of-done quality issues.",
            "evidence": issues,
            "impact": (
                "Compound or vague criteria are hard to verify objectively and invite scope drift."
            ),
            "suggested_fix": (
                "Split compound criteria, replace subjective terms with observable outcomes, "
                "and state how each is verified."
            ),
        },
        coverage=cov,
    )


# ── P7 destructive / irreversible sniff (advisory; escalates T4) ────────────────
_DESTRUCTIVE_RE = re.compile(
    r"\b(rm\s+-rf|drop\s+table|drop\s+database|truncate\s+table|delete\s+from|"
    r"force[- ]?push|push\s+--force|git\s+reset\s+--hard|reset\s+--hard|"
    r"DROP\s+COLUMN|destroy|wipe|purge)\b",
    re.IGNORECASE,
)
_SAFEGUARD_RE = re.compile(
    r"\b(backup|back up|snapshot|dry[- ]?run|reversible|rollback|roll back|restore|"
    r"soft[- ]?delete|idempotent|confirm|guard)\b",
    re.IGNORECASE,
)


def p7_destructive(ctx: PlanContext) -> DetResult:
    """Advisory. Sniff for destructive / irreversible operations stated without a
    nearby safeguard (backup/dry-run/rollback). Escalates the T4 overlay. Never
    blocks (it is a heuristic prompt to make the irreversibility an explicit,
    justified choice)."""
    from .det_floor import DetResult  # lazy: det_floor imports this module at load

    hits = sorted({m.group(0).lower() for m in _DESTRUCTIVE_RE.finditer(ctx.plan_text)})
    cov = {"ran": True, "destructive_hits": hits}
    if not hits:
        return DetResult("P7", "destructive-sniff", "pass", coverage=cov)
    has_safeguard = bool(_SAFEGUARD_RE.search(ctx.plan_text))
    if has_safeguard:
        cov["safeguard_present"] = True
        return DetResult("P7", "destructive-sniff", "pass", coverage=cov)
    return DetResult(
        "P7",
        "destructive-sniff",
        "fail",
        finding={
            "finding": "Destructive/irreversible operation(s) with no stated safeguard.",
            "evidence": [
                f"destructive terms: {', '.join(hits)}; no backup/dry-run/rollback nearby"
            ],
            "impact": "An irreversible op without a safeguard risks unrecoverable data/state loss.",
            "suggested_fix": (
                "State the safeguard (backup, dry-run, reversible migration, rollback) or "
                "justify the irreversibility explicitly (T4)."
            ),
        },
        coverage=cov,
    )
