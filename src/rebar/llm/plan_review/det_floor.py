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

    @property
    def has_children(self) -> bool:
        """Container (has children) vs leaf (none). This — NOT ticket type — is the
        proportionate-scrutiny axis: a childless epic is a leaf, a story with
        children is a container. See :func:`registry.applies`."""
        return bool(self.children)

    @property
    def plan_text(self) -> str:
        return f"{self.title}\n\n{self.description}"


# ── G5 decomposition signal (store-derived, task spangly-beggarly-blackrhino) ────────
# G5 (decomposition) once false-flagged an epic that already had 6 children as a "flat,
# undecomposed list" because it judged from ticket TEXT and counted children itself. The
# store already loads the real children (ctx.children). These two helpers make that fact
# authoritative:
#   * decomposition_state_block() — an authoritative child summary INJECTED into the G5
#     finder context (so the model never counts children itself);
#   * veto_undecomposed_g5() — a deterministic BACKSTOP that drops a residual G5
#     decomposition-ABSENCE finding when the ticket demonstrably has children.
# NOTE ON SEAM (why these are NOT DET_CHECKS entries): the DET floor (P1–P9) runs BEFORE
# the LLM tier produces any findings, so it cannot observe — let alone suppress — a G5
# finding. The veto is therefore applied POST-Pass-1, in run_pass1 (pass1.py), the only
# point where the model's finding and the store-derived child count are co-observable.
# These live here (with the other decomposition logic, e.g. p4_oversize) as pure,
# unit-testable helpers; run_pass1 calls them.
def decomposition_state_block(ctx: PlanContext) -> str:
    """The authoritative, store-sourced decomposition summary for the G5 finder context
    (AC1). Empty string when the ticket has no children (nothing authoritative to state —
    the finder judges decomposition from the plan as before). When children exist it names
    them as GROUND TRUTH so the finder never miscounts and never flags the ticket as
    flat/undecomposed."""
    if not ctx.children:
        return ""
    lines = [
        "## DECOMPOSITION STATE (from store)",
        (
            f"This ticket has {len(ctx.children)} direct child ticket(s) recorded in the "
            "store (authoritative ground truth — do NOT count children yourself, and do "
            "NOT flag this ticket as flat / undecomposed / monolithic / a single big list). "
            "Judge only the QUALITY of the decomposition (child altitude/content), not its "
            "existence:"
        ),
    ]
    for c in ctx.children:
        alias = c.get("alias") or c.get("ticket_id") or "?"
        title = (c.get("title") or "").strip()[:80]
        status = c.get("status") or (c.get("state") or {}).get("status") or "?"
        lines.append(f"  - {alias} — {title} ({status})")
    return "\n".join(lines)


# A decomposition-ABSENCE claim (the false-positive class the veto suppresses). Targets
# assertions that the ticket has NO decomposition / is flat / monolithic / should be
# broken up — deliberately NOT quality-of-decomposition language (wrong-altitude, poorly
# split children), so a genuine child-content/altitude G5 finding is preserved (AC2).
_G5_UNDECOMPOSED_RE = re.compile(
    r"(?:"
    r"undecompos\w*"
    r"|not\s+(?:yet\s+)?decomposed"
    r"|lacks?\s+(?:any\s+)?decomposition"
    r"|no\s+decomposition"
    r"|without\s+decomposition"
    r"|flat[,\s]+(?:and\s+)?(?:undecomposed|unstructured)"
    r"|flat\s+(?:list|structure|epic)"
    r"|monolithic"
    r"|(?:should|must|needs?\s+to|ought\s+to)\s+be\s+"
    r"(?:broken|split|decomposed|divided|carved|subdivided)"
    r"|break\s+(?:it|this|the\s+\w+)?\s*(?:down|up|into)"
    r"|split\s+into\s+(?:sub|child|smaller)"
    r"|no\s+(?:sub-?tasks|sub-?tickets|sub-?stories|children|child\s+tickets)"
    r"|single\s+(?:giant|large|huge|monolithic|undifferentiated)\s+"
    r"(?:ticket|epic|story|list|task)"
    r")",
    re.IGNORECASE,
)


def _is_undecomposed_claim(f: dict[str, Any]) -> bool:
    """True when a finding's prose asserts the ticket is NOT decomposed (the class the
    veto suppresses). Scans the finding + impact + suggested_fix text."""
    text = " ".join(str(f.get(k, "")) for k in ("finding", "impact", "suggested_fix"))
    return bool(_G5_UNDECOMPOSED_RE.search(text))


def veto_undecomposed_g5(
    findings: list[dict[str, Any]], ctx: PlanContext
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Deterministic backstop: drop any G5 decomposition-ABSENCE finding when the ticket
    demonstrably has children (count from ``ctx.children`` — the store — not the model).
    Returns ``(kept, vetoed)``.

    Applied POST-Pass-1 (see the seam note above). A no-op when the ticket has no children
    (a genuinely monolithic childless ticket still yields its G5 finding). Suppresses ONLY
    the absence subtype: a G5 finding about child ALTITUDE/CONTENT (children exist but are
    the wrong size/mix) does not match :data:`_G5_UNDECOMPOSED_RE` and is preserved."""
    if not ctx.children:
        return findings, []
    kept: list[dict[str, Any]] = []
    vetoed: list[dict[str, Any]] = []
    for f in findings:
        if "G5" in (f.get("criteria") or []) and _is_undecomposed_claim(f):
            vetoed.append(f)
        else:
            kept.append(f)
    return kept, vetoed


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


def _find_cycle(edges: dict[str, set[str]]) -> list[str] | None:
    """Return one cycle (as an id path) via DFS, or None. Deterministic order."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {n: WHITE for n in edges}
    stack: list[str] = []

    def visit(n: str) -> list[str] | None:
        color[n] = GRAY
        stack.append(n)
        for m in sorted(edges.get(n, ())):
            if color.get(m, WHITE) == GRAY:
                return stack[stack.index(m) :] + [m]
            if color.get(m, WHITE) == WHITE:
                got = visit(m)
                if got:
                    return got
        color[n] = BLACK
        stack.pop()
        return None

    for node in sorted(edges):
        if color[node] == WHITE:
            got = visit(node)
            if got:
                return got
    return None


def _file_interference(children: list[dict], edges: dict[str, set[str]]) -> list[str]:
    """Pairs of children sharing a file-impact path with no ordering edge between
    them (in either direction)."""
    paths: dict[str, list[str]] = {}
    for c in children:
        cid = c.get("ticket_id")
        if cid is None:
            continue
        for fi in c.get("file_impact", []) or []:
            p = fi.get("path") if isinstance(fi, dict) else fi
            if p:
                paths.setdefault(p, []).append(cid)
    out: list[str] = []
    for p, owners in sorted(paths.items()):
        uniq = sorted(set(owners))
        for i in range(len(uniq)):
            for j in range(i + 1, len(uniq)):
                a, b = uniq[i], uniq[j]
                if b in edges.get(a, ()) or a in edges.get(b, ()):
                    continue
                out.append(f"{p}: {a} & {b} (no ordering edge)")
    return out


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


def _ac_item_lines(text: str) -> list[str]:
    out, found = [], False
    for ln in text.split("\n"):
        if ln.lower().startswith("## acceptance criteria"):
            found = True
            continue
        if found and ln.startswith("## "):
            break
        if found and ln.startswith("- ["):
            out.append(ln)
    return out


# ── Operator-attested evidence-kind lint (R2, epic 6982; ADR-0043 × ADR-0016) ────
# Two work tickets (115b, 8c4f) burned close-gate cycles because their ACs had "done"
# evidence that inherently lives OUTSIDE the codebase (live-store fsck surgery, changes
# landed through Gerrit) but were not tagged [operator-attested] (ADR-0043), so the
# completion verifier failed hunting for code proof. This prompt-less DET lint (ADR-0016)
# surfaces that at PLAN time. It is ADVISORY — emitted through p6 (:func:`p6_ac_quality`),
# which never blocks — and is self-gated by the deterministic lexicon precision/recall eval
# in docs/experiments/plan-review-gate/ (see docs/plan-review-gate.md).
#
# The canonical [operator-attested] tag matcher (ADR-0043). OWNED here — the DET primitives
# module — and imported by workflow_ops (:data:`workflow_ops._OPERATOR_ATTESTED_AC_RE`) so
# the plan-time lint and the completion-verifier enrichment agree on "tagged" by construction.
# Matching is exact on the hyphenated token: a near-miss like [operator_attested] is NOT a match.
_OPERATOR_ATTESTED_TAG_RE = re.compile(
    r"^\s*-\s*\[[ xX]?\]\s*\[operator-attested\]\s*(.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# Operational-evidence marker families: an AC checklist item carrying one of these has
# "done" evidence that inherently lives OUTSIDE the code snapshot the completion verifier
# reads — a deploy, a prod/live-run outcome, an IaC apply, a cloud-resource state, a
# merge-gate (Gerrit vote) outcome, a human/operator action, an operator drill, live-store
# surgery, or a recorded out-of-band attestation. ADR-0043 wants such an AC tagged
# [operator-attested]. Same lexicon family as p6's vague-term lint and p7's destructive sniff.
_OPERATOR_EVIDENCE_MARKERS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pat, re.IGNORECASE))
    for name, pat in (
        (
            "deploy",
            r"\b(?:is|are|was|were)\s+deployed\b|\bdeployed\s+(?:\+\s*)?(?:and\s+)?"
            r"(?:activated|live|to|from|via)\b|\bpost-deploy\b|\bdeploys\s+(?:like|to|from)\b"
            r"|\bre-?deployed\b",
        ),
        (
            "prod",
            r"\bto\s+prod(?:uction)?\b|\bon\s+prod(?:uction)?\s+(?:host|env(?:ironment)?)\b"
            r"|\bon\s+the\s+box\b|\bon\s+the\s+running\s+\w+\b",
        ),
        (
            "live_run",
            r"\blive\s+(?:store|run|drill|traffic|e2e|dogfood|cutover|reconcile|smoke|box|Jira"
            r"|rebase|AWS|check)\b|\b(?:on|against)\s+the\s+live\s+(?:store|system|box|gerrit"
            r"|jira|environment|env|ruleset|shared|AWS)\b|\bgated\s+end-to-end\b|\bLive\s+E2E\b"
            r"|\bcanary\s+run\b",
        ),
        (
            "iac",
            r"\bterraform\s+(?:apply|plan|import)\b|\bcutover\s+applied\b|\blive\s+cutover\b"
            r"|\bimported\s+into\s+\w+\s+state\b|\bapply[^.\n]{0,25}refs/meta/config\b",
        ),
        (
            "cloud",
            r"\bcertbot\b|\bSNS\s+subscription\b|\bsubscription\s+(?:is\s+)?"
            r"(?:confirmed|delivers|delivering)\b|\bcomes?\s+up\s+clean\b|\bremain\s+in\s+AWS\b"
            r"|\bprovisioned\b|\bGitHub\s+Release\b|\bpublished\s+by\s+the\s+\w+\s+OIDC\b"
            r"|\bsystemd\s+unit\s+shows\b|\b(?:AWS\s+)?instance\s+.{0,30}"
            r"(?:provisioned|containerized)\b",
        ),
        (
            "merge_gate",
            r"\bland(?:ed|s)?\s+(?:on\s+`?main`?\s+)?(?:through|via)\s+(?:the\s+)?Gerrit\b"
            r"|\bmerged?\s+to\s+`?main`?\s+via\s+Gerrit\b|\blands?\s+on\s+`?main`?\b"
            r"(?![^.\n]*\btest\b)|\bsubmitted\s+in\s+Gerrit\b|\bpush(?:ed)?\s+to\s+Gerrit\b"
            r"|\bPR-merge\s+to\s+`?main`?\b|\breplicat\w+\s+`?main`?\s+to\b",
        ),
        (
            "human",
            r"\bmanual\s+(?:apply|step|copy|process|window)\b|\bone-time\s+manual\b"
            r"|\bby\s+hand\b|\bhuman\s+triage\b|\bquiet\s+window\b|\bthe\s+operator\s+"
            r"(?:creates|configures|applies|runs|obtains)\b|\bnot\s+(?:by\s+hand|manually)\b"
            r"|\bcreated\s+\**automatically\**\s+by\s+the\s+workflow\b",
        ),
        ("drill", r"\boperator[ -]?drill\b|\bgame[ -]?day\b|\bfire[ -]?drill\b"),
        (
            "store_op",
            r"\bphantom\s+dir\w*\b|\bretire\s+(?:each\s+)?(?:of\s+)?(?:the\s+)?\d+\b"
            r"|\.retired`?\s+file\b|\bagainst\s+the\s+(?:live\s+)?shared\s+store\b"
            r"|\bagainst\s+the\s+live\s+store\b|\bLive\s+store\s+reaches\b",
        ),
        (
            "attested",
            r"\battested\s+by\s+recorded\b|\brecord(?:ed)?\s+(?:the\s+)?"
            r"(?:change\s+id|vote\s+outcome|close-event\s+ids?|command\s+output|counts\s+on\s+this)\b"
            r"|\bvote\s+history\s+at\b|\bcounts\s+on\s+this\s+ticket\b",
        ),
    )
)

# Codebase-verifiable SUPPRESSION co-signal: the item proves itself IN-REPO — it names a
# proving command, a test, a doc, or a config-as-file deliverable, or carries a
# "<code deliverable> — landed on main" trailer — so it is NOT operator-attested evidence
# even when a marker word appears. Precision-first by design: an advisory lint must not nag.
_OPERATOR_EVIDENCE_SUPPRESS = re.compile(
    r"`(?:grep|egrep|rg|pytest|test\s+-[fedxz]|jq|python|ls|cat|diff|make|node)\b"
    r"|\bgrep\s+-|\bpytest\b|\btest\s+-f\b"
    r"|\b(?:unit|regression|property|convergence|synthetic-fixture|fault-injection|e2e)"
    r"\s+tests?\b"
    r"|\btests?\s+(?:that|asserts?|drives?|replays?|proves?|pins?|exercises?|guards?)\b"
    r"|\bNEW\s+test\b|\bRECALL\s+fixture\b|\btest\s+change\b"
    r"|\bdocuments?\b|\bdocumented\b|\bADR\s+records\b"
    r"|\brecords?\s+the\s+(?:three|novel|decision|rationale)\b"
    r"|\bconfig(?:uration)?\s+(?:file|key)s?\b"
    r"|—\s+landed\s+on\s+main|-\s+landed\s+on\s+main",
    re.IGNORECASE,
)
# Explicit negation: the item states it does NOT touch a live/deployed surface.
_OPERATOR_EVIDENCE_NEGATION = re.compile(
    r"\bno\s+live\b|\bwithout\s+(?:a\s+)?live\b|\bnot\s+(?:a\s+)?deploy"
    r"|\bno\s+(?:live\s+)?store/repo\b|\bNO\s+staged-rollout\b|\bread-only\s+provisioning\b"
    r"|\bno\s+change\s+created\b",
    re.IGNORECASE,
)


def _operator_evidence_ac_gaps(text: str) -> list[tuple[str, list[str]]]:
    """Pure detector for the operator-attested-evidence lint (R2). Returns one
    ``(ac_line, marker_names)`` per AC checklist item that (a) is NOT already tagged
    ``[operator-attested]``, (b) carries >=1 operational-evidence marker, and (c) is not
    suppressed by a codebase-verifiable co-signal or an explicit negation. Deterministic,
    side-effect-free, LLM-free — this is the unit the R2 self-gate evaluates and the coaching
    :func:`p6_ac_quality` surfaces. Returns ``[]`` when the plan has no such gap."""
    gaps: list[tuple[str, list[str]]] = []
    for line in _ac_item_lines(text):
        if _OPERATOR_ATTESTED_TAG_RE.match(line):
            continue  # already tagged — its out-of-codebase evidence is declared
        if _OPERATOR_EVIDENCE_NEGATION.search(line) or _OPERATOR_EVIDENCE_SUPPRESS.search(line):
            continue  # codebase-verifiable / negated — not an operational-evidence gap
        hits = [name for name, rx in _OPERATOR_EVIDENCE_MARKERS if rx.search(line)]
        if hits:
            gaps.append((line, hits))
    return gaps


# ── Verify-command lint (G-3a, epic cite-stone-sea / WS4) ───────────────────────
# A DETERMINISTIC, mechanically-checkable lint over the verify/proving commands a plan states,
# catching three classes of a "present command that silently lies". PRIOR ART: shellcheck is the
# reference lint here — SC2126 (a file-inspection pipeline that asserts nothing), SC2062/SC2063
# (an unquoted/unanchored grep pattern), SC2086 (an unquoted `$var` the shell expands before the
# tool sees it). We keep a BESPOKE, dependency-free, LANGUAGE-AGNOSTIC subset rather than shell out
# to shellcheck (bash-only + a heavy external dep, and verify commands here are polyglot), and we
# FAIL OPEN: a command in a shape we cannot confidently parse ABSTAINS (recorded in coverage) rather
# than being false-accused. This is advisory (P6 never blocks); the LLM tier (E6) catches the
# judgement-requiring defects (fixture validity, cardinality, wrong assertion target).
_VERIFY_INSPECTION_VERBS = ("grep", "egrep", "fgrep", "find", "ls", "wc", "cat", "head", "tail")
# An assertion construct that turns an inspection into a real check (exit code / comparison).
_VERIFY_ASSERTION_RE = re.compile(
    r"(-eq|-ne|-gt|-lt|-ge|-le|==|!=|\s-q\b|\s-c\b|\btest\b|\[\s|\[\[|&&|\|\||\bexit\b|"
    r"\bjq\s+-e\b|\bdiff\b|\bcmp\b|\s-z\b|\s-n\b)"
)
_GREP_RE = re.compile(r"\b(grep|egrep|fgrep)\b")
# A bare-identifier grep pattern (quoted or not) with no anchoring/structure around it.
_GREP_BARE_WORD_RE = re.compile(
    r"\b(?:grep|egrep|fgrep)(?:\s+-\w+)*\s+'?([A-Za-z_][A-Za-z0-9_]*)'?(?:\s|$)"
)
# ^ $ [] () \ : " — any of these anywhere in the command reads as "anchored / structured".
_GREP_ANCHOR_CHARS = re.compile(r"[\^$\[\]()\\:\"]")
_SHELL_VAR_RE = re.compile(r"\$\w+|\$\{|\$\?")


def _lint_verify_command(cmd: str) -> tuple[str | None, bool]:
    """Lint ONE verify/proving command. Returns ``(defect_msg, abstained)``:
    a defect string (the command mechanically lies), or ``abstained=True`` (shape we cannot
    parse — fail-open, coverage-recorded), or ``(None, False)`` when the command is clean."""
    c = (cmd or "").strip().strip("`")
    if not c:
        return None, True
    tokens = c.split()
    verb = tokens[0] if tokens else ""
    is_grep = bool(_GREP_RE.search(c))
    if verb not in _VERIFY_INSPECTION_VERBS and not is_grep:
        return None, True  # not a shape this bespoke lint understands → fail-open abstain
    if is_grep:
        # (3) unquoted shell variable in the grep command — expands before grep sees it (SC2086).
        if _SHELL_VAR_RE.search(c):
            return (
                f"verify command `{c}` has an unquoted shell variable ($VAR/$?) in its grep — it "
                "expands before grep runs; the pattern is not what you wrote",
                False,
            )
        # (2) unanchored grep — a bare-word pattern substring-matches (SC2062): `grep cycle`
        # matches `review_cycle`. Anchor with \\b / ^$ / a quoted key.
        m = _GREP_BARE_WORD_RE.search(c + " ")
        if m and not _GREP_ANCHOR_CHARS.search(c):
            w = m.group(1)
            return (
                f"verify command `{c}` greps the unanchored word '{w}' — it substring-matches "
                f"(e.g. 'review_{w}'); anchor it (\\b, ^$, or a quoted key like '\"{w}\":')",
                False,
            )
    # (1) file-inspection with no assertion — `cat out.json` proves nothing (SC2126); add an
    # exit-code / comparison (-q, [ ... -eq N ], jq -e).
    if verb in _VERIFY_INSPECTION_VERBS and not _VERIFY_ASSERTION_RE.search(c):
        return (
            f"verify command `{c}` inspects output with `{verb}` but asserts nothing — add an "
            'exit-code or comparison (-q, [ "$(...)" -eq N ], jq -e) so a failure is observable',
            False,
        )
    return None, False


def _verify_command_strings(ctx: PlanContext) -> list[str]:
    """The verify/proving commands to lint: the structured `verify_commands` (each entry's
    `command`) PLUS command-shaped lines the plan states inline (`Verify:` / `Proof:` prose and
    backtick-fenced commands in AC items) — the 'present command that lies' defect shows up in
    both channels."""
    out: list[str] = []
    for entry in ctx.state.get("verify_commands") or []:
        cmd = entry.get("command") if isinstance(entry, dict) else None
        if cmd:
            out.append(str(cmd))
    for ln in ctx.plan_text.split("\n"):
        m = re.search(r"(?:Verify|Proof)\s*:\s*(.+)", ln, re.IGNORECASE)
        if m:
            out.append(m.group(1).strip())
        out.extend(re.findall(r"`([^`]+)`", ln))  # backtick-fenced commands
    return out


def p6_ac_quality(ctx: PlanContext) -> DetResult:
    """Advisory. Lexical AC quality checks: compound-AND criteria (one item
    bundling multiple deliverables joined by ' and '), vague/subjective lexicon,
    and whether any verification command/section is present. Never blocks."""
    items = _ac_item_lines(ctx.plan_text)
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
    # OUTSIDE the codebase (deploy/prod/live-run/infra/merge-gate/human/attestation) but which are
    # NOT tagged [operator-attested]. ADVISORY coaching only (p6 never blocks); each gap's fix is
    # inline. Self-gated by the deterministic lexicon eval (docs/experiments/plan-review-gate/).
    oa_gaps = _operator_evidence_ac_gaps(ctx.plan_text)
    for line, markers in oa_gaps:
        subject = re.sub(r"^\s*-\s*\[[ xX]?\]\s*", "", line).strip()[:80]
        issues.append(
            f"AC item {subject!r} cites operational evidence ({', '.join(markers)}) that lives "
            "outside the codebase but is not tagged [operator-attested]; prefix the checkbox text "
            "with [operator-attested] so the completion verifier accepts a recorded attestation "
            "instead of failing to find code proof (ADR-0043)."
        )
    cov = {
        "ran": True,
        "ac_items": len(items),
        "verify_commands_linted": len(linted),
        "verify_lint_abstained": lint_abstained,
        "operator_attested_gaps": len(oa_gaps),
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
