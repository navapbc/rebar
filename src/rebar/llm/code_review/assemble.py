"""Diff context-assembler for the four-pass code-review gate.

Parallel to plan-review's ``assemble_context(...).plan_text``: it turns a
``(base, head)`` git range — or a unified-diff string + changed-files list — into the
single ``context`` string the kernel Pass-2 verifier re-grounds findings against. For
code review that context IS the diff (the change set is the focus), shaped as a
``## Changed files`` / ``## Orientation`` / ``## Diff`` markdown composite.

This module is deliberately SELF-CONTAINED — it RE-IMPLEMENTS the small git diff-read
helpers rather than importing them from the single-pass ``code_review.single_pass`` route,
so WS4's retirement of that route requires NO change to this module. A test pins this at the
SOURCE level (no import of a single-pass symbol); the package ``__init__`` is kept light and
lazily re-exports the single-pass API, so importing this module does not pull that route in
at package-import time. (Completing the runtime decoupling — making ``import rebar.llm`` stop
eagerly importing the single-pass route — is WS4's source-separation work.)
"""

from __future__ import annotations

import os
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field

from rebar._store.gitutil import run_git
from rebar.llm.errors import LLMConfigError

# Keep the inlined diff bounded; the verify/finder agents read files for the rest. A config
# value (not a magic wire constant) — adjustable without changing the context shape.
DIFF_CHAR_CAP = 60000


def _git(repo: str, args: list[str]) -> str:
    """Run a git subcommand in ``repo``, returning stdout (raises :class:`LLMConfigError`
    on failure with an actionable hint to pass ``diff_text`` instead)."""
    try:
        proc = run_git(repo, *args, check=False, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise LLMConfigError(f"git failed ({' '.join(args)}): {exc}") from exc
    if proc.returncode != 0:
        raise LLMConfigError(
            f"git {' '.join(args)} failed: {proc.stderr.strip()}; pass diff_text= "
            "and changed_files= to assemble a diff context without a git range."
        )
    return proc.stdout


def changed_from_diff(diff_text: str) -> list[str]:
    """Changed paths parsed from a unified diff. Reads ``diff --git a/… b/…`` headers (the
    new path — covers deletions/renames) and falls back to ``+++ b/`` for non-git diffs;
    skips ``/dev/null``; order-preserving + de-duplicated."""
    files: list[str] = []
    seen: set[str] = set()
    for line in diff_text.splitlines():
        path = None
        if line.startswith("diff --git ") and " b/" in line:
            path = line.split(" b/", 1)[1].strip()
        elif line.startswith("+++ b/"):
            path = line[6:].strip()
        if path and path != "/dev/null" and path not in seen:
            seen.add(path)
            files.append(path)
    return files


def _orientation(changed_files: list[str]) -> str:
    by_dir: dict[str, list[str]] = defaultdict(list)
    for f in changed_files:
        by_dir[os.path.dirname(f) or "."].append(os.path.basename(f))
    lines = ["Changed files grouped by directory (use your tools to read neighbors):"]
    for d in sorted(by_dir):
        lines.append(f"  {d}/: " + ", ".join(sorted(by_dir[d])))
    return "\n".join(lines)


def compose_diff_context(
    changed_files: list[str], diff_text: str, *, diff_char_cap: int = DIFF_CHAR_CAP
) -> str:
    """The kernel ``context`` string for a code change: a changed-files list, a
    directory-grouped orientation, and the unified diff (capped, with a truncation
    notice when it exceeds ``diff_char_cap``)."""
    diff = (
        diff_text
        if len(diff_text) <= diff_char_cap
        else (diff_text[:diff_char_cap] + "\n…(diff truncated; use your file tools for the rest)")
    )
    return (
        f"## Changed files ({len(changed_files)})\n"
        + "\n".join(changed_files or ["(none)"])
        + f"\n\n## Orientation\n{_orientation(changed_files)}"
        + f"\n\n## Diff\n```diff\n{diff}\n```"
    )


# ── merge-change context (epic 88ab / S2) ────────────────────────────────────
# A merge change is reviewed on ONLY its auto-merge delta (conflict resolutions) plus the
# list of commits it integrates — never the whole feature diff (R1). Gerrit's synthetic
# pseudo-paths are not real conflict files and are excluded from the auto-merge diff.
_MAGIC_MERGE_PATHS = frozenset({"/COMMIT_MSG", "/MERGE_LIST"})

#: COUNT pre-cap on the integrated-commit list (a huge feature branch could integrate
#: thousands of commits — the subject list is bounded before the char cap applies).
MERGELIST_MAX_COMMITS = 100


def assemble_merge_change_context(
    merge_files: dict[str, dict],
    file_diffs: dict[str, str],
    mergelist: list[dict],
    *,
    diff_char_cap: int = DIFF_CHAR_CAP,
    mergelist_max_commits: int = MERGELIST_MAX_COMMITS,
) -> str:
    """The kernel ``diff_text`` for a MERGE change: a ``## Merge context`` section (the
    integrated-commit subjects, COUNT-capped at ``mergelist_max_commits`` with a truncation
    notice) followed by a ``## Auto-merge diff`` section (the per-file conflict-resolution
    diffs, magic pseudo-paths excluded).

    ``merge_files`` (from ``get_merge_files``) is an explicit input: it names the real
    auto-merge files, paired with their fetched diff text in ``file_diffs``. Cap
    apportionment: ``diff_char_cap`` governs the COMBINED string — the merge-context section
    is laid down first (already count-bounded), then the auto-merge diff is truncated last to
    fit the remaining budget. A CLEAN merge (no real files) renders an explicit empty-delta
    notice and the review proceeds on the mergelist context alone."""
    # 1. Merge context — integrated-commit subjects (count-capped).
    total = len(mergelist)
    shown = mergelist[:mergelist_max_commits]
    ctx = [f"## Merge context ({total} integrated commit(s))"]
    for c in shown:
        sha = str(c.get("commit") or "")[:10]
        subj_raw = c.get("subject") or ""
        subj = subj_raw.splitlines()[0] if subj_raw else ""
        ctx.append(f"- {sha} {subj}".rstrip())
    if total > mergelist_max_commits:
        ctx.append(f"…({total - mergelist_max_commits} more integrated commit(s) omitted)")
    merge_ctx = "\n".join(ctx)

    # 2. Auto-merge diff — real files only (magic pseudo-paths excluded), in file-map order.
    real_files = [f for f in (merge_files or {}) if f not in _MAGIC_MERGE_PATHS]
    diff_blocks: list[str] = []
    for f in real_files:
        d = (file_diffs or {}).get(f)
        if d:
            diff_blocks.append(f"### {f}\n{d}")
    auto_diff = "\n\n".join(diff_blocks)

    # 3. Combined cap — merge context first (count-bounded), diff truncated last.
    if not auto_diff:
        diff_section = (
            "## Auto-merge diff\n(empty — the auto-merge produced no conflict delta "
            "(clean merge). Review is on the integrated-commit context above.)"
        )
    else:
        budget = max(0, diff_char_cap - len(merge_ctx) - len("\n\n## Auto-merge diff\n"))
        body = (
            auto_diff
            if len(auto_diff) <= budget
            else auto_diff[:budget]
            + "\n…(auto-merge diff truncated; use your file tools for the rest)"
        )
        diff_section = f"## Auto-merge diff\n{body}"
    return f"{merge_ctx}\n\n{diff_section}"


@dataclass(frozen=True)
class DiffContext:
    """Everything the code-review gate needs about a change under review — the
    code-review analogue of plan-review's ``PlanContext``. ``context`` is the string the
    Pass-2 verifier re-grounds against (parallel to ``PlanContext.plan_text``)."""

    diff_text: str
    changed_files: list[str] = field(default_factory=list)
    base: str | None = None
    head: str | None = None
    repo_root: str | None = None
    diff_char_cap: int = DIFF_CHAR_CAP

    @property
    def context(self) -> str:
        """The kernel ``context`` string (see :func:`compose_diff_context`)."""
        return compose_diff_context(
            self.changed_files, self.diff_text, diff_char_cap=self.diff_char_cap
        )


def assemble_diff_context(
    *,
    base: str = "HEAD~1",
    head: str = "HEAD",
    diff_text: str | None = None,
    changed_files: list[str] | None = None,
    repo_root: str | None = None,
    diff_char_cap: int = DIFF_CHAR_CAP,
) -> DiffContext:
    """Build a :class:`DiffContext` from a git range OR a supplied unified diff.

    When ``diff_text`` is None the diff + changed files are read via git
    (``git diff base..head`` from the real checkout's object DB — a pinned snapshot tree
    has no history). When ``diff_text`` is supplied (the offline/test seam), ``changed_files``
    is parsed from it if not given. The ``(base, head)`` range is recorded on the context.
    """
    if diff_text is None:
        # Resolve the object DB via the standard config precedence (repo_root arg >
        # REBAR_ROOT env > git toplevel), NOT a snapshot tree (which lacks .git history).
        from rebar import config as _config

        diff_repo = str(_config.repo_root(repo_root))
        diff_text = _git(diff_repo, ["diff", f"{base}..{head}"])
        # `--name-only` is newline-delimited; splitlines (NOT split) so paths with spaces
        # survive intact. Drop blank lines.
        changed_files = [
            ln
            for ln in _git(diff_repo, ["diff", "--name-only", f"{base}..{head}"]).splitlines()
            if ln
        ]
    elif changed_files is None:
        changed_files = changed_from_diff(diff_text)
    return DiffContext(
        diff_text=diff_text,
        changed_files=list(changed_files or []),
        base=base,
        head=head,
        repo_root=repo_root,
        diff_char_cap=diff_char_cap,
    )
