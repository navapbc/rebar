"""Code-review operation: review a change (commits/diff) with multiple reviewers.

Diff-centric (the change set is the focus) but agentic — each reviewer is the same
tool-using agent as ``review_ticket``, so it pulls surrounding context on demand
rather than seeing the diff in isolation. Reviewer selection is deterministic
(``code-quality`` always, plus any catalog reviewer whose ``applies_to`` globs match
the changed files), each reviewer runs as its own pass, and the per-reviewer
findings are merged by :func:`rebar.llm.aggregate.aggregate_findings`
(cluster → consensus → rank). A lightweight repo "orientation" seeds the agent
with the changed-file layout (full tree-sitter/PageRank repo-map is a future
enhancement).
"""

from __future__ import annotations

import os
import subprocess
from collections import defaultdict

from rebar.llm import prompts
from rebar.llm.aggregate import aggregate_findings
from rebar.llm.config import LLMConfig
from rebar.llm.errors import LLMConfigError
from rebar.llm.findings import build_result, resolve_citations, validate_result
from rebar.llm.runner import Runner, RunRequest, get_runner

__all__ = ["review_code", "select_code_reviewers"]

_DIFF_CHAR_CAP = 60000  # keep the inlined diff bounded; the agent reads files for more


def select_code_reviewers(changed_files: list[str]) -> list[str]:
    """Deterministic reviewer set for a code change: ``code-quality`` always, plus
    any catalog reviewer whose ``applies_to`` globs match a changed file (e.g.
    ``security`` for auth paths, ``tests`` for test files). Excludes the
    ticket-only default reviewer."""
    catalog = prompts.load_catalog()
    ids = ["code-quality"] if "code-quality" in catalog else []
    for rid in prompts.select_reviewers(changed_files):
        if rid not in ids and catalog[rid].applies_to:  # skip default-only reviewers
            ids.append(rid)
    return ids


def _git(repo: str, args: list[str]) -> str:
    try:
        proc = subprocess.run(["git", "-C", repo, *args], capture_output=True,
                              text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise LLMConfigError(f"git failed ({' '.join(args)}): {exc}") from exc
    if proc.returncode != 0:
        raise LLMConfigError(
            f"git {' '.join(args)} failed: {proc.stderr.strip()}; pass diff_text= "
            "and changed_files= to review without a git range."
        )
    return proc.stdout


def _changed_from_diff(diff_text: str) -> list[str]:
    """Changed paths from a unified diff. Parse ``diff --git a/… b/…`` headers (the
    new path) so deletions (``+++ /dev/null``) and renames are covered too — not just
    ``+++ b/`` lines — and fall back to ``+++ b/`` for non-git diffs. /dev/null is
    skipped; order-preserving + de-duplicated."""
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


def _compose_context(changed_files: list[str], diff_text: str) -> str:
    diff = diff_text if len(diff_text) <= _DIFF_CHAR_CAP else (
        diff_text[:_DIFF_CHAR_CAP] + "\n…(diff truncated; use your file tools for the rest)"
    )
    return (
        f"## Changed files ({len(changed_files)})\n" + "\n".join(changed_files or ["(none)"])
        + f"\n\n## Orientation\n{_orientation(changed_files)}"
        + f"\n\n## Diff\n```diff\n{diff}\n```"
    )


def review_code(
    *,
    base: str = "HEAD~1",
    head: str = "HEAD",
    diff_text: str | None = None,
    changed_files: list[str] | None = None,
    reviewers: list[str] | None = None,
    repo_root=None,
    config: LLMConfig | None = None,
    runner: Runner | None = None,
) -> dict:
    """Review a code change with one or more reviewers and return an aggregated
    ``review_result``.

    Provide a git range (``base``/``head``) — the diff and changed files are read
    via git — or pass ``diff_text`` (+ optional ``changed_files``) to review without
    a git range (also the test seam). ``reviewers`` overrides the deterministic
    selection. Findings from all reviewers are merged (cluster → consensus → rank);
    each carries ``agreement`` + ``reviewers``.
    """
    cfg = config or LLMConfig.from_env(repo_root=repo_root)
    repo = cfg.repo_path or "."
    if diff_text is None:
        diff_text = _git(repo, ["diff", f"{base}..{head}"])
        changed_files = _git(repo, ["diff", "--name-only", f"{base}..{head}"]).split()
    elif changed_files is None:
        changed_files = _changed_from_diff(diff_text)

    reviewer_ids = reviewers or select_code_reviewers(changed_files)
    context = _compose_context(changed_files, diff_text)
    selected = get_runner(cfg, override=runner)

    results: list[dict] = []
    for rid in reviewer_ids:
        reviewer = prompts.get_reviewer(rid)
        variables = {"ticket_id": "(code review)", "ticket_context": context,
                     "repo_path": cfg.repo_path or ""}
        system_prompt, lf_prompt = prompts.resolve_prompt(reviewer, variables, cfg.langfuse)
        instructions = (
            f"Review the code change above along the '{reviewer.dimension}' dimension. "
            "USE your read-only file tools to read the changed files and their context "
            "(don't review the diff in isolation); cite real `path:line` from read_file "
            "output and never invent locations. Return findings via the structured output."
        )
        req = RunRequest(
            system_prompt=system_prompt, instructions=instructions, config=cfg,
            reviewers=[rid],
            target={"kind": "code", "files": changed_files},
            langfuse_prompt=lf_prompt,
        )
        results.append(selected.run(req))

    merged = aggregate_findings(results)
    runner_name = results[0]["runner"] if results else getattr(selected, "name", cfg.runner)
    result = build_result(
        merged,
        runner=runner_name,
        model=cfg.model,
        trace_id=results[0].get("trace_id") if results else None,
        target={"kind": "code", "commits": [base, head], "files": changed_files},
        reviewers=reviewer_ids,
        summary=f"{len(reviewer_ids)} reviewer(s); {len(merged)} finding(s) after aggregation.",
    )
    resolve_citations(result, cfg.repo_path)
    return validate_result(result)
