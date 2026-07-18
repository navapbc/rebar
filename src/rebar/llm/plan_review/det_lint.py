"""Pure, DetResult-free deterministic helpers for the plan-review DET floor.

Extracted from :mod:`.det_floor` (which owns the P1-P9 static floor and the
``DET_CHECKS`` registry) so that grandfathered, size-ceilinged module does not keep
growing. This module is a pure leaf: it imports only the stdlib ``re`` and references
:class:`.det_floor.PlanContext` solely as a type annotation, pulled in under
``TYPE_CHECKING`` (with ``from __future__ import annotations`` first) so there is NO
runtime import back into ``det_floor`` — ``det_floor``'s import of these helpers is
therefore cycle-free.

It carries three cohesive islands of behavior-preserving helpers:

* the **G5 decomposition** signal (``decomposition_state_block`` /
  ``veto_undecomposed_g5`` and their private matcher), which ``det_floor`` re-exports
  and ``pass1.py`` calls POST-Pass-1;
* the pure **task-DAG graph helpers** (``_find_cycle`` / ``_file_interference``), which
  ``det_floor.p5_task_dag`` imports and calls; and
* the **verify-command lint** (``_lint_verify_command`` / ``_verify_command_strings``
  and their patterns), which ``det_floor.p6_ac_quality`` imports and surfaces.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .det_floor import PlanContext


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
# These live in this pure-helper leaf, imported and re-exported by :mod:`.det_floor`;
# run_pass1 calls them.
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


# ── task-DAG graph helpers (pure; imported + called by det_floor.p5_task_dag) ────────
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
