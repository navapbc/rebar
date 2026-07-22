"""Layer-1 deterministic floor (P1–P9) for the plan-review gate (child 012e).

The DET floor is the ONLY tier that blocks **by default** in v1 (every LLM-tier
criterion is advisory unless a project opts it into blocking via its
``block_threshold``). It is a frozen, deterministic, polyglot floor that **fails
open** on any unsupported stack: a check that cannot run records an ``abstain``
(with a reason) and is treated as PASS — the recorded abstain set IS the coverage.

It is a first-class ``exec=DET`` execution tier the orchestrator dispatches via a
CODE executor (no LLM, no network round-trip beyond the optional grounding-oracle
lanes), alongside the 1-TURN / 2-STEP / AGENT (LLM) tiers.

The checks
----------
* **P1 readiness-shape** — requires an ``## Acceptance Criteria`` checklist (the
  universal floor ``check_ac``/``clarity_check`` enforce). **BLOCKS** when absent.
* **P2 file/symbol/import resolution** — probes explicit ``path/like.ext`` and
  symbol references in the plan against the code-grounding oracle
  (:func:`rebar.grounding.refute_absence`, universal-ctags T1). Coverage only,
  **never blocks** (a plan legitimately references files it will *create*).
* **P3 package existence** — probes explicit dependency references against the
  oracle's T0 deps lane. Coverage only, **never blocks**.
* **P4 oversize signals** — a plan-size heuristic (AC count / file-impact count /
  description length). Advisory finding, **never blocks**. (``scc``/``lizard``
  code metrics apply to code-review, epic ``9da1`` — a plan has no diff to size.)
* **P5 task-DAG validity + interference** — for a container, detects dependency
  **cycles** among children (**BLOCKS** — sound + unambiguous) and file-impact
  interference between unordered children (advisory).
* **P6 AC/DD quality** — lexical checks (compound-AND criteria, vague lexicon,
  verify-command presence). Advisory, **never blocks**.
* **P7 destructive/irreversible sniff** — scans for destructive operations stated
  without a safeguard (escalates the T4 overlay). Advisory, **never blocks**.
* **P8 reviewability / context-budget** — a token-estimate check: **BLOCKS** when
  the content (or, for a container, a parent+child pairing) exceeds the largest
  configured context window even at one-criterion-per-call ("too big to review in
  full; reduce/decompose it" — the extreme of P4 / G5).
* **P9 file-impact coverage** — for a LEAF work ticket, warns (advisory, **never
  blocks**) when ``file_impact`` is empty: without it the code-drift gate (ADR 0002)
  cannot scope the attestation and falls back to invalidating on any commit.

The only sound, unambiguous blockers are therefore **P1, P5 (cycle), and P8**.
Everything else is advisory or coverage-only, consistent with "the DET floor
blocks only on sound, unambiguous checks and fails open on everything else".
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from .det_lint import (
    _file_interference,
    _find_cycle,
    _lint_verify_command,
    _verify_command_strings,
    decomposition_state_block,  # noqa: F401 — re-exported for pass1.py + test_g5_decomp_det
    veto_undecomposed_g5,  # noqa: F401 — re-exported for pass1.py + test_g5_decomp_det
)

logger = logging.getLogger(__name__)

# ── token budgeting ───────────────────────────────────────────────────────────
# Cheap char/4 heuristic (matches the experiment harness `est_tokens`); the gate
# never relies on an exact count, only on a generous budget comparison.
CHARS_PER_TOKEN = 4
# Largest context window we will escalate to (Opus/Sonnet 1M). Config-overridable
# via the orchestrator; P8 fails only when content exceeds this even one-at-a-time.
DEFAULT_LARGEST_WINDOW_TOKENS = 1_000_000
# Reserve headroom for the system prompt + rubric + output on the biggest call.
P8_OUTPUT_RESERVE_TOKENS = 32_000
P8_HEADROOM = 0.9


def est_tokens(text: str | None) -> int:
    """Cheap token estimate (chars / 4). Never raises."""
    return len(text or "") // CHARS_PER_TOKEN


@dataclass(frozen=True)
class DetResult:
    """One DET check outcome.

    ``status`` is ``pass`` (check ran, clean), ``fail`` (check ran, found a
    defect), or ``abstain`` (check could not run — fail-open, treated as pass).
    ``blocking`` is True only for a *blocking* fail (P1/P5-cycle/P8). ``finding``
    carries the structured defect on a fail. ``coverage`` records whether the
    check actually ran and why (so the attestation can report completeness)."""

    id: str
    name: str
    status: str  # "pass" | "fail" | "abstain"
    blocking: bool = False
    finding: dict[str, Any] | None = None
    coverage: dict[str, Any] = field(default_factory=dict)

    @property
    def blocked(self) -> bool:
        return self.status == "fail" and self.blocking


@dataclass
class PlanContext:
    """Everything the DET floor (and the orchestrator) needs about the ticket
    under review. Assembled once from rebar's own reads — the content is ALWAYS
    whole (no truncation, no content-chunking, by design)."""

    ticket_id: str
    ticket_type: str
    title: str
    description: str
    state: dict[str, Any] = field(default_factory=dict)
    children: list[dict[str, Any]] = field(default_factory=list)
    repo_root: str | None = None
    # The TICKET-STORE read root — distinct from ``repo_root`` (the CODE root). In an
    # attested gate the ticket store lives on the orphan ``tickets`` branch and is
    # materialized SEPARATELY (``current_tickets_root()`` / ``cfg.tickets_path``), so it
    # is ABSENT from the code snapshot. Downstream ticket reads (linked session logs,
    # prior REVIEW_RESULT concerns) MUST resolve against this root, not ``repo_root`` —
    # else ``tracker_dir(<code-snapshot>)`` points at a missing ``.tickets-tracker`` and
    # the read spuriously "cannot list"s / silently drops context. Captured on the
    # assembling thread (where the ContextVar is set) so it survives the pass-1 worker
    # threads that a ContextVar would NOT be inherited by. ``None`` → the live checkout
    # store (local / non-attested), which is the correct default there.
    tickets_root: str | None = None
    largest_window_tokens: int = DEFAULT_LARGEST_WINDOW_TOKENS
    # Centrality / blast-radius signal in [0,1], computed at plan time from the ticket
    # graph (dependents + children) — scales review depth + the budget cap (a central,
    # high-blast-radius plan earns more scrutiny). 0 = a leaf nobody depends on.
    centrality: float = 0.0
    # Hierarchy-load completeness (ticket b24d): True when child enumeration or a per-child
    # fetch exhausted its retries — the review must never reach a clean PASS on a plan whose
    # hierarchy context is known-incomplete. ``hierarchy_incomplete_detail`` records WHICH
    # read(s) failed: the literal "enumeration" for a total list_tickets failure, or the
    # failing child's ticket id for a per-child show_ticket failure (possibly several).
    hierarchy_incomplete: bool = False
    hierarchy_incomplete_detail: list[str] = field(default_factory=list)

    @property
    def has_children(self) -> bool:
        """Container (has children) vs leaf (none). This — NOT ticket type — is the
        proportionate-scrutiny axis: a childless epic is a leaf, a story with
        children is a container. See :func:`registry.applies`."""
        return bool(self.children)

    @property
    def plan_text(self) -> str:
        return f"{self.title}\n\n{self.description}"


# ── P1 readiness-shape ─────────────────────────────────────────────────────────
def _count_ac_items(text: str) -> int:
    """`- [ ]` / `- [x]` checklist items under `## Acceptance Criteria`
    (reset on the next `## ` heading). Mirrors gates._count_ac_reset so the DET
    floor shares the exact vocabulary of the standalone check_ac gate."""
    count, found = 0, False
    for ln in text.split("\n"):
        if ln.lower().startswith("## acceptance criteria"):
            found = True
            continue
        if found and ln.startswith("## "):
            found = False
            continue
        if found and ln.startswith("- ["):
            count += 1
    return count


def p1_readiness_shape(ctx: PlanContext) -> DetResult:
    """BLOCKING. The universal floor: a ticket must carry an
    ``## Acceptance Criteria`` checklist with ≥1 item, across all types. Clarity
    (a heuristic) is recorded as coverage but does NOT block (it can false-fail)."""
    text = ctx.plan_text
    n = _count_ac_items(text)
    clarity = _clarity_score(ctx.description, ctx.ticket_type)
    cov = {"ran": True, "ac_items": n, "clarity_score": clarity}
    if n >= 1:
        return DetResult("P1", "readiness-shape", "pass", coverage=cov)
    return DetResult(
        "P1",
        "readiness-shape",
        "fail",
        blocking=True,
        finding={
            "finding": (
                "The ticket has no `## Acceptance Criteria` checklist. A plan cannot be "
                "reviewed for completion without testable, checkable criteria."
            ),
            "evidence": ["No `## Acceptance Criteria` section with `- [ ]` items found."],
            "impact": "The plan is not dispatchable: no objective definition of done.",
            "suggested_fix": (
                "Add an `## Acceptance Criteria` section with `- [ ]` checklist items, one per "
                "observable, in-session-verifiable outcome."
            ),
        },
        coverage=cov,
    )


def _clarity_score(description: str, ticket_type: str) -> int:
    """A copy of gates._clarity_score's heuristic (structure + per-type headings),
    recorded as P1 coverage. Kept local so the DET floor never imports a CLI gate
    transitively, but intentionally identical in vocabulary."""
    score = 0
    if re.search(r"^##\s+\S", description, re.MULTILINE):
        score += 1
    if len(description) >= 200:
        score += 1
    if len(description) >= 500:
        score += 1
    if re.search(r"^- ", description, re.MULTILINE):
        score += 1
    if ticket_type == "task":
        if re.search(r"^##\s+Acceptance Criteria", description, re.MULTILINE | re.IGNORECASE):
            score += 2
        if re.search(r"(?:^|\s)[\w./]+/[\w./]+", description, re.MULTILINE):
            score += 1
    elif ticket_type == "story":
        has_why = bool(re.search(r"^##\s+Why\b", description, re.MULTILINE | re.IGNORECASE))
        has_what = bool(re.search(r"^##\s+What\b", description, re.MULTILINE | re.IGNORECASE))
        if has_why and has_what:
            score += 2
        if re.search(r"^##\s+Scope\b", description, re.MULTILINE | re.IGNORECASE):
            score += 1
    elif ticket_type == "epic":
        if re.search(r"^##\s+Success Criteria", description, re.MULTILINE | re.IGNORECASE):
            score += 2
        if re.search(r"^##\s+Context\b", description, re.MULTILINE | re.IGNORECASE):
            score += 1
    return score


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


# ── P4 oversize signals (plan-size heuristic, advisory) ────────────────────────
P4_AC_SOFT_CAP = 25  # checklist items
P4_FILE_IMPACT_SOFT_CAP = 30  # file-impact entries
P4_DESC_SOFT_CAP_CHARS = 24_000


def p4_oversize(ctx: PlanContext) -> DetResult:
    """Advisory. A plan-size heuristic: an unusually large AC count / file-impact
    set / description length is a *signal* the unit may be too big for one session
    (the deterministic precursor to the G5 decomposition judgment). Never blocks —
    sizing is ultimately a judgment call P8 backstops with a hard limit."""
    ac = _count_ac_items(ctx.plan_text)
    fi = len(ctx.state.get("file_impact") or [])
    chars = len(ctx.description)
    signals = []
    if ac > P4_AC_SOFT_CAP:
        signals.append(f"{ac} acceptance-criteria items (> {P4_AC_SOFT_CAP})")
    if fi > P4_FILE_IMPACT_SOFT_CAP:
        signals.append(f"{fi} file-impact entries (> {P4_FILE_IMPACT_SOFT_CAP})")
    if chars > P4_DESC_SOFT_CAP_CHARS:
        signals.append(f"description is {chars} chars (> {P4_DESC_SOFT_CAP_CHARS})")
    cov = {"ran": True, "ac_items": ac, "file_impact": fi, "desc_chars": chars}
    if not signals:
        return DetResult("P4", "oversize", "pass", coverage=cov)
    return DetResult(
        "P4",
        "oversize",
        "fail",
        finding={
            "finding": "Oversize signals suggest this unit may be too large for one session.",
            "evidence": signals,
            "impact": (
                "Large units compound early errors and are hard to one-shot; "
                "consider G5 decomposition."
            ),
            "suggested_fix": "Split into smaller child tickets, each a coherent single outcome.",
        },
        coverage=cov,
    )


# ── P5 task-DAG validity + interference (container; cycle BLOCKS) ───────────────
def p5_task_dag(ctx: PlanContext) -> DetResult:
    """For a container (has_children): detect dependency **cycles** among the
    children (BLOCKING — a cycle is sound + unambiguous) and file-impact
    interference between children with no ordering edge (advisory). A leaf ticket
    is a natural no-op pass."""
    if not ctx.has_children:
        return DetResult("P5", "task-dag", "pass", coverage={"ran": True, "children": 0})
    child_ids = {c.get("ticket_id") for c in ctx.children}
    # Build the intra-child dependency edges (depends_on / blocks), restricted to
    # the child set, from each child's deps list.
    edges: dict[str, set[str]] = {cid: set() for cid in child_ids if cid}
    for c in ctx.children:
        cid = c.get("ticket_id")
        if cid is None:
            continue
        for dep in c.get("deps", []) or []:
            tgt = dep.get("target_id")
            rel = dep.get("relation")
            if tgt not in child_ids:
                continue
            if rel == "depends_on":
                edges.setdefault(cid, set()).add(tgt)
            elif rel == "blocks":
                edges.setdefault(tgt, set()).add(cid)
    cycle = _find_cycle(edges)
    cov = {"ran": True, "children": len(child_ids), "edges": sum(len(v) for v in edges.values())}
    if cycle:
        return DetResult(
            "P5",
            "task-dag",
            "fail",
            blocking=True,
            finding={
                "finding": "The child dependency graph contains a cycle.",
                "evidence": [" → ".join(cycle)],
                "impact": "A dependency cycle is unschedulable: no child can start first.",
                "suggested_fix": "Break the cycle by removing or re-pointing one dependency edge.",
            },
            coverage=cov,
        )
    # File-impact interference: two children touching the same path with no edge.
    interference = _file_interference(ctx.children, edges)
    if interference:
        return DetResult(
            "P5",
            "task-dag",
            "fail",
            finding={
                "finding": "Sibling tickets touch the same file(s) with no ordering edge.",
                "evidence": interference[:10],
                "impact": (
                    "Unordered file overlap risks merge conflicts / lost work when run in parallel."
                ),
                "suggested_fix": (
                    "Add a depends_on/blocks edge to serialize, or partition the file ownership."
                ),
            },
            coverage=cov,
        )
    return DetResult("P5", "task-dag", "pass", coverage=cov)


# ── P6 AC/DD quality (lexical, advisory) ───────────────────────────────────────
_VAGUE_LEXICON = (
    "better",
    "improved",
    "improve",
    "sufficient",
    "robust",
    "robustly",
    "appropriate",
    "appropriately",
    "properly",
    "reasonable",
    "as needed",
    "etc.",
    "and so on",
    "good",
    "clean",
    "nice",
    "optimal",
    "efficient",
)


def p6_ac_quality(ctx: PlanContext) -> DetResult:
    """Advisory. Lexical AC quality checks: compound-AND criteria (one item
    bundling multiple deliverables joined by ' and '), vague/subjective lexicon,
    and whether any verification command/section is present. Never blocks."""
    from . import det_citation, det_operator_attested

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
    vague_hits = sorted({w for w in _VAGUE_LEXICON if re.search(rf"\b{re.escape(w)}", low)})
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
    # Cross-ticket citation edge-verify lint (story 266e): a `[rebar:<id>]` citation whose
    # cited id is not a VERIFIED upstream prerequisite of P (P.depends_on(C) or C.blocks(P)).
    # ADVISORY coaching only (p6 never blocks); Layer-2 (the LLM finders) owns crediting.
    # Reverse lookup is fail-closed inside det_citation. Logic lives in det_citation (det_floor
    # is size-ceilinged); this is just the call + extend.
    from rebar import _reads

    def _resolve_deps(cid: str) -> list[dict[str, Any]]:
        return _reads.show_ticket(cid, repo_root=ctx.tickets_root).get("deps", []) or []

    cit_issues = det_citation.unbacked_citations(
        det_citation.parse_citations(ctx.plan_text),
        ctx.state.get("deps", []) or [],
        _resolve_deps,
        ctx.ticket_id,
    )
    issues.extend(cit_issues)
    cov = {
        "ran": True,
        "ac_items": len(items),
        "verify_commands_linted": len(linted),
        "verify_lint_abstained": lint_abstained,
        "operator_attested_gaps": len(oa_issues),
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


# ── P8 reviewability / context-budget (BLOCKS when too big) ─────────────────────
def p8_reviewability(ctx: PlanContext) -> DetResult:
    """BLOCKING. The size backstop: fails when the content — or, for a container,
    a parent+largest-child pairing — exceeds the largest configured context window
    even at one-criterion-per-call (minimal rubric + full content). That is "too
    big to review in full; reduce/decompose it" (the extreme of P4 / G5). Content
    is never chunked, so when it cannot fit even alone the only sound outcome is to
    require the author to reduce it."""
    budget = int(ctx.largest_window_tokens * P8_HEADROOM) - P8_OUTPUT_RESERVE_TOKENS
    plan_tokens = est_tokens(ctx.plan_text)
    cov: dict[str, Any] = {"ran": True, "plan_tokens": plan_tokens, "budget_tokens": budget}
    over: list[str] = []
    if plan_tokens > budget:
        over.append(f"plan is ~{plan_tokens} tokens (> budget ~{budget})")
    # Container: each (parent + one child) pairing must fit (G3/G4 review one child
    # at a time, both whole).
    if ctx.has_children:
        worst = 0
        for c in ctx.children:
            pair = plan_tokens + est_tokens(f"{c.get('title', '')}\n{c.get('description', '')}")
            worst = max(worst, pair)
        cov["worst_parent_child_pair_tokens"] = worst
        if worst > budget:
            over.append(f"the largest parent+child pairing is ~{worst} tokens (> budget ~{budget})")
    if not over:
        return DetResult("P8", "reviewability", "pass", coverage=cov)
    return DetResult(
        "P8",
        "reviewability",
        "fail",
        blocking=True,
        finding={
            "finding": "The ticket is too large to review in full, even one criterion at a time.",
            "evidence": over,
            "impact": (
                "A plan that exceeds the largest context window cannot be reviewed whole; "
                "any review would see a partial plan."
            ),
            "suggested_fix": (
                "Reduce or decompose the ticket (and/or its children) so the content fits a "
                "single review pass."
            ),
        },
        coverage=cov,
    )


# ── P9 file-impact coverage (advisory; epic boil-golem-veto / ADR 0002) ──────────
def p9_file_impact_coverage(ctx: PlanContext) -> DetResult:
    """Advisory. A LEAF work ticket with no ``file_impact`` cannot have its plan-review
    attestation scoped to specific files, so the code-drift gate (ADR 0002) falls back
    to invalidating on ANY commit, and ``next_batch`` cannot schedule it conflict-free.
    Surfaces a coaching nudge to declare the files; NEVER blocks. Not applicable to
    containers (anything with children) or non-work types, where ``file_impact`` is
    legitimately absent — those pass."""
    fi = ctx.state.get("file_impact") or []
    # Applicable to any LEAF (no children) — a leaf of any type is a work ticket that
    # should scope its attestation. Container tickets pass (file_impact legitimately
    # lives on their children). Bug/session_log are gate-exempt upstream, so they never
    # reach the DET floor — no ticket-type gate is needed here.
    applicable = not ctx.children
    cov = {"ran": True, "file_impact": len(fi), "applicable": applicable}
    if not applicable or fi:
        return DetResult("P9", "file-impact-coverage", "pass", coverage=cov)
    return DetResult(
        "P9",
        "file-impact-coverage",
        "fail",
        finding={
            "finding": "No file_impact declared on a leaf work ticket.",
            "evidence": ["file_impact is empty"],
            "impact": (
                "The plan-review attestation cannot be scoped to specific files, so ANY "
                "commit invalidates it (the conservative code-drift fallback, ADR 0002), "
                "and next_batch cannot schedule this ticket conflict-free."
            ),
            "suggested_fix": (
                "Record the {path, reason} files this work will touch (e.g. via "
                "set_file_impact) so the attestation is scoped to them."
            ),
        },
        coverage=cov,
    )


# ── the floor ──────────────────────────────────────────────────────────────────
DET_CHECKS = (
    p1_readiness_shape,
    p2_resolution,
    p3_package_existence,
    p4_oversize,
    p5_task_dag,
    p6_ac_quality,
    p7_destructive,
    p8_reviewability,
    p9_file_impact_coverage,
)


def run_det_floor(ctx: PlanContext) -> list[DetResult]:
    """Run the two-phase deterministic floor, fail-open per check:

    1. the STATIC built-in floor (P1–P9, :data:`DET_CHECKS`) — the frozen, polyglot readiness
       floor, in order;
    2. the DYNAMIC project-invariant phase (:func:`det_invariants.run_project_det_checks`) — the
       activated ``exec: "DET"`` project criteria from the ``.rebar/`` overlay (empty ⇒ zero
       results, so the floor is byte-identical for a repo with no project DET criterion).

    An unexpected error in a check becomes an ``abstain`` (logged), never an exception that aborts
    the floor — for both phases."""
    results: list[DetResult] = []
    for check in DET_CHECKS:
        try:
            results.append(check(ctx))
        except Exception as exc:  # noqa: BLE001 — fail-open: a broken check abstains, never blocks; broad-but-logged with the traceback
            # A DET check raising is an internal bug (not an expected fail-open like an
            # absent oracle): record the abstain in-band AND log it with the traceback so
            # the broken check is observable, not silently swallowed.
            logger.warning("DET check %s raised; abstaining", check.__name__, exc_info=True)
            results.append(
                DetResult(
                    check.__name__.split("_")[0].upper(),
                    check.__name__,
                    "abstain",
                    coverage={"ran": False, "reason": f"error:{exc}"},
                )
            )
    # Phase 2: the dynamic project-DET phase (its own per-criterion fail-open). Imported lazily so
    # det_floor carries no import-time dependency on the registry/grounding stack.
    try:
        from .det_invariants import run_project_det_checks

        results.extend(run_project_det_checks(ctx))
    except Exception:  # noqa: BLE001 — fail-open: the whole project-DET phase degrades to nothing, logged
        logger.warning("project DET phase raised; skipping", exc_info=True)
    return results


def det_finding_has_subject(finding: dict) -> bool:
    """a8e5 Component 2 — a DET finding is ADJUDICABLE only if it names a concrete subject: a
    non-blank ``location`` OR at least one ``evidence`` span. A subject-less DET finding (no
    location, no evidence) is unadjudicable ("Sibling tickets touch the same file(s)" naming no
    tickets/files) and is dropped by the hygiene backstop at the DET emission point. All existing
    DET checks emit evidence, so this drops nothing in practice — it is a safety net."""
    return bool((finding.get("location") or "").strip()) or bool(finding.get("evidence"))


def det_blocking_findings(results: list[DetResult]) -> list[dict]:
    """The blocking findings from a DET run (P1/P5-cycle/P8), each tagged with its
    criterion id — the orchestrator surfaces these as the gate's hard blocks. Subject-less
    DET findings are dropped by the hygiene backstop (:func:`det_finding_has_subject`)."""
    out = []
    for r in results:
        if r.blocked and r.finding:
            if not det_finding_has_subject(r.finding):
                logger.warning("dropping subject-less blocking DET finding from %s", r.name)
                continue
            out.append({**r.finding, "criteria": [r.id], "criterion_name": r.name, "tier": "DET"})
    return out


def det_advisory_findings(results: list[DetResult]) -> list[dict]:
    """The non-blocking DET findings (P4/P6/P7 + P5 interference), surfaced as advisory
    coaching alongside the LLM-tier advisory set. Subject-less DET findings are dropped by the
    hygiene backstop (:func:`det_finding_has_subject`) — this is DET-scoped by construction
    (LLM-tier findings never flow through this function)."""
    out = []
    for r in results:
        if r.status == "fail" and not r.blocking and r.finding:
            if not det_finding_has_subject(r.finding):
                logger.warning("dropping subject-less advisory DET finding from %s", r.name)
                continue
            out.append({**r.finding, "criteria": [r.id], "criterion_name": r.name, "tier": "DET"})
    return out


def det_coverage(results: list[DetResult]) -> dict[str, Any]:
    """The coverage record for the attestation: per-check ran/abstain + detail."""
    return {
        r.id: {"name": r.name, "status": r.status, "blocking": r.blocking, **r.coverage}
        for r in results
    }
