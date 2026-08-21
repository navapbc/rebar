"""Deterministic criterion pre-gates for the plan-review routing seam (ticket 4ee2).

Owns :class:`DetGateRule` and the two rule tables :func:`registry` re-exports:

* ``_DET_OVERLAY_RULES`` — the deterministic OVERLAY triggers (T-prefixed criteria).
  T5a/T5d/T7/T12 are the original low-FP keyword triggers (behavior-preserved as
  one-element ``text_all`` rules); T13/T14 are the ADR-0034-amendment pre-filters for
  the two LLM-routed enumeration overlays that produced zero findings in 1,939
  agentic runs each.
* ``_DET_LEAF_GATE_RULES`` — the parallel table for the two LEAF-routed (non-overlay)
  zero-finding criteria, ``removal-rationale`` (1,722 runs) and
  ``asserted-capability`` (409 runs), gated by a second block in
  ``orchestrator.route_criteria`` (the overlay guard's ``is_overlay`` check cannot
  serve them).

The T13/T14/removal-rationale/asserted-capability trigger regexes are transcribed
VERBATIM from the adversarial false-negative audit (provenance: ticket 696a); their
amended fire rates over 743 reviewed non-bug tickets are 84.0% / 61.6% / 86.5% /
96.5%. The contract (recorded as the amendment note in
``docs/adr/0034-llm-routed-enumeration-overlays.md``): these gates are conservative
PRE-FILTERS, not applicability judges — a plan skips a criterion only on TOTAL
subject-vocabulary absence; a fired plan still routes through the unchanged LLM
router, which remains the applicability authority. A not-fired gate performs zero
LLM routing for that criterion and is recorded in ``route_criteria``'s ``gate_log``
(surfaced in the sidecar as ``coverage.routing.det_gated``).

This module was split out of :mod:`.registry` along its existing overlay-triggering
seam because the verbatim trigger vocabularies would push ``registry.py`` past the
800-LOC module cap; ``registry`` re-exports every public name, so all existing
``registry.overlay_triggers`` / ``registry._DET_OVERLAY_RULES`` call sites are
unchanged.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

# The ticket model's file_impact entries are ``{path, reason}`` dicts — there is no
# deleted-file flag, so the removal-detection arm is a regex over the REASON (an author
# declaring removal work states it there) and the path; no other deletion signal exists
# and none is invented.
FileImpact = Sequence[Mapping[str, Any]]


@dataclass(frozen=True)
class DetGateRule:
    """One deterministic criterion pre-gate. The rule FIRES — i.e. the criterion
    proceeds to its normal LLM routing — when ANY arm matches:

    * ``text_all``: regexes over the plan text, ALL of which must match (conjunction
      support — single-leg rules use a one-element tuple; asserted-capability uses two:
      the module-ref leg and the capability-verb leg);
    * ``file_impact_globs``: fnmatch patterns over each declared file_impact entry's
      ``path``;
    * ``file_impact_reason_re``: a regex over each file_impact entry's ``reason`` and
      ``path`` strings.

    ``name`` is the identifier recorded in ``gate_log`` / ``coverage.routing.det_gated``
    when the rule does NOT fire."""

    name: str
    text_all: tuple[str, ...] = ()
    file_impact_globs: tuple[str, ...] = ()
    file_impact_reason_re: str | None = None

    def fires(self, plan: str, file_impact: FileImpact | None = None) -> bool:
        p = plan or ""
        if self.text_all and all(re.search(rx, p, re.IGNORECASE) for rx in self.text_all):
            return True
        entries = [e for e in (file_impact or []) if isinstance(e, Mapping)]
        if self.file_impact_globs:
            for e in entries:
                path = str(e.get("path") or "")
                if any(fnmatch.fnmatch(path, g) for g in self.file_impact_globs):
                    return True
        if self.file_impact_reason_re:
            rx = re.compile(self.file_impact_reason_re, re.IGNORECASE)
            for e in entries:
                if rx.search(str(e.get("reason") or "")) or rx.search(str(e.get("path") or "")):
                    return True
        return False


# ── deterministic OVERLAY triggers (low-FP where deterministic; T13/T14/T10 pre-filters) ──
# The rest of the overlays (T6/T5b/T9 + the agent-tier T1/T3/T5c/T8/T11) are
# LLM-routed at Pass-1 (a keyword trigger is high-FP for them), so they are NOT listed.
_DET_OVERLAY_RULES: dict[str, DetGateRule] = {
    "T5a": DetGateRule(
        "perf-vocabulary",
        (r"\b(latency|throughput|performance|scal\w*|n\+1|batch|cache|memory|hot[- ]?path)\b",),
    ),
    "T5d": DetGateRule(
        "ui-vocabulary",
        (r"\b(ui|button|form|screen|page|modal|wcag|aria|accessib\w*|keyboard|contrast)\b",),
    ),
    "T7": DetGateRule(
        "docs-vocabulary",
        (r"\b(\bdocs?\b|readme|claude\.md|adr|guide|documentation)\b",),
    ),
    "T12": DetGateRule(
        "deploy-vocabulary",
        (r"\b(deploy|rollout|canary|feature flag|production traffic|rollback|blue.green)\b",),
    ),
    # T13 prohibition-enumeration: mechanism-of-enforcement vocabulary (audit-amended),
    # UNIONED with the additive-obligation lexicon (story 5bca-4ca9 P2: T13's trigger widened
    # from prohibition to obligation, so additive-only phrasings must reach the LLM router).
    # The enforcement branch stays byte-for-byte audit-verbatim; the additive branch is a
    # separate alternation appended to it.
    "T13": DetGateRule(
        "mechanism-of-enforcement",
        (
            r"\b(?:block\w*|reject\w*|refus\w*|enforc\w*|den(?:y|ies|ied)\b|forbid\w*|prohibit\w*|disallow\w*|outlaw\w*|must (?:pass|not)\b|cannot (?:merge|close|claim|submit|land|push)|fail(?:s|ing)? (?:the )?(?:build|ci|check|gate)|gate[sd]? (?:on|behind)|require\w*[^.\n]{0,60}\bbefore\b|no longer (?:allowed|permitted|accepts?)|restrict\w*|bounce\w*|veto\w*|\bbar(?:s|red|ring)\b|exit(?:s|ing)?\s+(?:code\s+)?(?:non-?zero|[0-9]+)|\bnon-?zero\b|raises?\s+\w*(?:Error|Exception)|\bunless\b|prerequisite\w*|precondition\w*|hard\s+(?:requirement|stop|fail)|only\s+[^.\n]{0,40}\b(?:may|can|are\s+allowed)\b|now\s+(?:needs|requires)|turns?\s+(?:the\s+)?(?:run|build|ci)\s+red|goes\s+red|\bgate\w*\b|\bhook\w*\b)"  # noqa: E501 — audit-verbatim trigger regex (ticket 696a); do not rewrap
            # additive-obligation lexicon (story 5bca-4ca9 P2)
            r"|\b(?:adds?|adding|introduces?|gains?)\s+(?:an?\s+|the\s+)?(?:new\s+)?"
            r"(?:required|mandatory)\b|\bcallers?\s+must\b|\bevery\s+\w+\s+must\b"
            r"|\bmust\s+(?:stay|be\s+kept|remain)\s+in\s+(?:sync|step)\b"
            r"|\bnew\s+(?:contract|invariant|obligation|schema\s+field)\b",
        ),
    ),
    # T10 infra/IaC: the audited infra-vocabulary pre-filter (ticket bfa8; measured
    # 28% fire rate with 100% strong-finding recall). Text-only — no file_impact arms.
    "T10": DetGateRule(
        "infra-vocabulary",
        (
            r"\b(?:terraform|cloudformation|pulumi|ansible|kubernetes|helm|k8s|iac|aws|ec2|s3|iam|ssm|cloudwatch|vpc|sns|lambda|rds|ebs|docker|compose|infra/|provision\w*|systemd|launchagent|user_data|cloudfront|api gateway|oidc|\.github/workflows|github actions|workflow_dispatch|pull_request_target|secrets\.[A-Z_]+)\b",  # noqa: E501 — audit-verbatim trigger regex (ticket bfa8); do not rewrap
        ),
    ),
    # T14 CI-trigger / release-infra: scheduler/release vocabulary + a structured
    # file_impact condition over CI/build-surface paths (audit-amended).
    "T14": DetGateRule(
        "ci-trigger-surface",
        (
            r"(?:\.github/workflows|\bworkflows?\b|workflow_dispatch|pull_request_target|\bcron\b|schedul\w*|refs?/(?:for|heads|tags|meta)|branch(?:es)?\s*[:=]|branch (?:pattern|namespace|filter)|git ref|tag (?:push|pattern|creation)|release (?:script|pipeline|process|workflow|job)|new (?:ci )?job|entry[- ]?point|ci (?:job|trigger|run|pipeline|gate|workflow)|github actions|package (?:publish|release)|pypi|gerrit-verify|\bnightly\b|\bweekly\b|\bhourly\b|\blaunchd\b|launch\s?agent|dependabot|\brunner\b|\bmakefile\b|pre-(?:push|commit|receive)|commit-msg|console[-_ ]scripts?|\bwheel\b|\bsdist\b|publish\w*|upload\w*[^.\n]{0,40}\b(?:index|registry|artifact)\b|on\s+(?:every\s+)?(?:push|merge|tag)\b|\boidc\b|docker-compose|\bcompose\b|mirror[- ]guard|\bnamespace\b|lands?\s+on\b)",  # noqa: E501 — audit-verbatim trigger regex (ticket 696a); do not rewrap
        ),
        file_impact_globs=(
            ".github/*",
            "Makefile",
            "*.pre-commit*",
            "hooks/*",
            "*/hooks/*",
            "pyproject.toml",
            "*.yml",
            "*.yaml",
        ),
    ),
}

# ── deterministic LEAF gates (the non-overlay zero-finding pair) ──────────────────────
_DET_LEAF_GATE_RULES: dict[str, DetGateRule] = {
    # Removal/weakening vocabulary (audit-amended), plus the reason/path removal arm.
    "removal-rationale": DetGateRule(
        "removal-vocabulary",
        (
            r"\b(?:remov\w*|delet\w*|drop\w*|retir\w*|deprecat\w*|prun\w*|strip\w*|weaken\w*|relax\w*|loosen\w*|eliminat\w*|decommission\w*|sunset\w*|rip(?:ping)? out|dead[- ]code|no longer|obsolet\w*|unus\w*|clean[- ]?up|simplif\w*|consolidat\w*|replac\w*|disabl\w*|bypass\w*|skip\w*|suppress\w*|silenc\w*|\bmut(?:e|ed|ing)\b|swallow\w*|demot\w*|downgrad\w*|soften\w*|supersed\w*|subsum\w*|\bfold(?:s|ed|ing)?\b|collaps\w*|inlin\w*|absorb\w*|\bno[- ]?op\b|waiv\w*|opt[- ]?outs?|turn(?:s|ed|ing)?\s+off|switch(?:es|ed|ing)?\s+off|shrink\w*|narrow\w*|exempt\w*|\bignor\w*|single\s+attempt|fail[- ]fast|\bbecomes?\s+a?\s*warning\b)",  # noqa: E501 — audit-verbatim trigger regex (ticket 696a); do not rewrap
        ),
        file_impact_reason_re=r"\b(?:remov|delet|drop|retir|decommission|sunset|prun)\w*",
    ),
    # Asserted-capability: a two-leg CONJUNCTION — a module reference AND a
    # capability verb must BOTH be present (audit-amended, two regex bug fixes).
    "asserted-capability": DetGateRule(
        "module-capability-conjunction",
        (
            r"(?:[\w/]+\.(?:py|sh|ts|js|go|rs)\b|\b[a-z_]\w*\.[a-z_]\w*\.[a-z_]\w*\b|\b[a-z]+(?:_[a-z0-9]+)+\b|\b(?:module|helper|function|class|subsystem|registry|reducer|adapter|client|store|layer|service|component|pipeline|engine|wrapper|utils?|library|suite|script|hook|gate|sidecar|verifier|reviewer|table|command|test|check|flag)\b)",
            r"\b(?:already\b|exist\w*|lean(?:s|ing)?\s+on|lives?\s+in|\bcovers?\b|takes?\s+care|responsible\s+for|does not (?:have|support|provide|exist|emit|handle)|doesn'?t (?:have|support|provide|exist)|lacks?\b|missing\b|no existing\b|must be (?:built|added|created)|not (?:yet )?(?:present|implemented|supported)|(?:provides|supports|exposes|implements|emits|handles|owns)\b|can(?:not|'t)?\s+(?:handle|do|parse|emit|sign)|(?:won'?t|will\s+not|never|only)\s+\w+|reus\w*|extend\w*|delegates?\s+to|calls?\s+into|nothing\s+in\b|wire\w*)",  # noqa: E501 — audit-verbatim trigger regex (ticket 696a); do not rewrap
        ),
    ),
}


def project_trigger_fires(
    trigger: object | Any,
    plan: str,
    file_impact: FileImpact | None = None,
) -> bool | None:
    """Evaluate a project criterion's deterministic ``trigger`` list (from
    ``.rebar/criteria_routing.json``) against *plan* and *file_impact*.

    Returns:
    - ``None``  (fail-open) when *trigger* is absent/None, not a non-empty list, or ANY
      entry is malformed (not a Mapping; or no usable arm after compiling regexes).
    - ``True``  when EVERY entry's :class:`DetGateRule` fires (conjunction across entries).
    - ``False`` when at least one entry fires nothing.

    A regex that fails to compile makes its entry malformed → None (fail-open).
    Never raises. Ticket a584, fail-open contract: None must not gate.
    """
    if not trigger or not isinstance(trigger, Sequence) or isinstance(trigger, str):
        return None
    rules: list[DetGateRule] = []
    for entry in trigger:
        if not isinstance(entry, Mapping):
            return None
        try:
            raw_text_all = entry.get("text_all") or []
            raw_globs = entry.get("file_impact_globs") or []
            raw_reason_re = entry.get("file_impact_reason_re") or None
            text_all = tuple(str(r) for r in raw_text_all) if raw_text_all else ()
            globs = tuple(str(g) for g in raw_globs) if raw_globs else ()
            reason_re = str(raw_reason_re) if raw_reason_re else None
            # Validate: at least one usable arm
            if not text_all and not globs and not reason_re:
                return None
            # Compile-check all regexes defensively
            for rx in text_all:
                re.compile(rx)
            if reason_re:
                re.compile(reason_re)
        except (re.error, TypeError, ValueError):
            return None
        rules.append(DetGateRule("project-trigger", text_all, globs, reason_re))
    if not rules:
        return None
    return all(r.fires(plan, file_impact) for r in rules)


def overlay_triggers(plan: str, file_impact: FileImpact | None = None) -> dict[str, bool]:
    """Deterministic overlay triggers. Returns ``{overlay_id: fired}`` for every rule in
    ``_DET_OVERLAY_RULES``; the remaining overlays are LLM-routed and absent from this
    map. ``file_impact`` (the ticket's declared ``{path, reason}`` entries) feeds the
    T14 glob arm; the historical text-only call shape still works."""
    return {ov: rule.fires(plan, file_impact) for ov, rule in _DET_OVERLAY_RULES.items()}


def leaf_gate_triggers(plan: str, file_impact: FileImpact | None = None) -> dict[str, bool]:
    """Deterministic LEAF-criterion gates — the ``_DET_LEAF_GATE_RULES`` analog of
    :func:`overlay_triggers` for the non-overlay pair (removal-rationale /
    asserted-capability), evaluated by ``route_criteria``'s second gate block."""
    return {cid: rule.fires(plan, file_impact) for cid, rule in _DET_LEAF_GATE_RULES.items()}
