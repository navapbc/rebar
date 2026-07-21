"""Operator-attested evidence-kind advisory DET lint (R2, ticket b080, epic 6982;
ADR-0043 x ADR-0016).

Two work tickets (115b, 8c4f) burned close-gate cycles because their ACs had "done"
evidence that inherently lives OUTSIDE the codebase (live-store fsck surgery, changes
landed through Gerrit) but were not tagged ``[operator-attested]`` (ADR-0043), so the
completion verifier failed hunting for code proof. This prompt-less DET lint (ADR-0016)
surfaces that at PLAN time. It is ADVISORY — surfaced through ``p6_ac_quality``
(:mod:`.det_floor`), which never blocks — and is self-gated by the deterministic lexicon
precision/recall eval in docs/experiments/plan-review-gate/ (see docs/plan-review-gate.md).

Extracted from ``det_floor`` (which owns the P1-P9 static floor) so that grandfathered,
size-ceilinged module does not grow; ``det_floor.p6_ac_quality`` imports and surfaces this
lint. This module is a pure leaf (stdlib ``re`` only) — it imports nothing from ``det_floor``,
so ``det_floor``'s import of it is cycle-free, and :data:`_OPERATOR_ATTESTED_TAG_RE` is the
single source :mod:`.workflow_ops` re-exports so the plan-time lint and the completion-verifier
enrichment agree on "tagged" by construction.
"""

from __future__ import annotations

import re


def ac_item_lines(text: str) -> list[str]:
    """Return the ``- [ ]`` checklist item lines under a plan's ``## Acceptance Criteria``
    heading (until the next ``##`` heading). Shared by :func:`p6_ac_quality` and the lint below."""
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


# The canonical [operator-attested] tag matcher (ADR-0043). OWNED here and re-exported by
# workflow_ops (:data:`workflow_ops._OPERATOR_ATTESTED_AC_RE`) so the plan-time lint and the
# completion-verifier enrichment agree on "tagged" by construction. Matching is exact on the
# hyphenated token: a near-miss like [operator_attested] is NOT a match.
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


def operator_evidence_ac_gaps(ac_lines: list[str]) -> list[tuple[str, list[str]]]:
    """Pure detector for the operator-attested-evidence lint (R2). Given AC checklist item
    lines (as produced by :func:`ac_item_lines`), returns one ``(ac_line, marker_names)`` per
    item that (a) is NOT already tagged ``[operator-attested]``, (b) carries >=1
    operational-evidence marker, and (c) is not suppressed by a codebase-verifiable co-signal
    or an explicit negation. Deterministic, side-effect-free, LLM-free — this is the unit the
    R2 self-gate evaluates. Returns ``[]`` when there is no such gap."""
    gaps: list[tuple[str, list[str]]] = []
    for line in ac_lines:
        if _OPERATOR_ATTESTED_TAG_RE.match(line):
            continue  # already tagged — its out-of-codebase evidence is declared
        if _OPERATOR_EVIDENCE_NEGATION.search(line) or _OPERATOR_EVIDENCE_SUPPRESS.search(line):
            continue  # codebase-verifiable / negated — not an operational-evidence gap
        hits = [name for name, rx in _OPERATOR_EVIDENCE_MARKERS if rx.search(line)]
        if hits:
            gaps.append((line, hits))
    return gaps


def operator_evidence_issues(ac_lines: list[str]) -> list[str]:
    """Advisory coaching strings (one per gap) for :func:`p6_ac_quality` to surface. Each
    carries its fix inline. Never blocks (p6 is advisory)."""
    issues: list[str] = []
    for line, markers in operator_evidence_ac_gaps(ac_lines):
        subject = re.sub(r"^\s*-\s*\[[ xX]?\]\s*", "", line).strip()[:80]
        issues.append(
            f"AC item {subject!r} cites operational evidence ({', '.join(markers)}) that lives "
            "outside the codebase but is not tagged [operator-attested]; prefix the checkbox text "
            "with [operator-attested] so the completion verifier accepts a recorded attestation "
            "instead of failing to find code proof."
        )
    return issues
