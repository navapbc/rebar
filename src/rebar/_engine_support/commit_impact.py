"""Deterministic commit <-> file-impact utilities (no LLM, no ``rebar.llm`` import).

Two responsibilities, both shared by every surface that needs them:

* **Commit-SHA validation** for the ``attach-commits`` repair surface — used by the
  ``rebar.attach_commits`` seam so the CLI, the Python library, and the MCP tool all
  inherit identical, ALL-OR-NOTHING validation semantics.
* **Path matching** for the close gate's file-impact-vs-diff check — the ``**/``-aware
  glob semantics that previously lived only inside the optional ``[agents]`` LLM
  package (``rebar.llm.prompting.prompts._glob_match``, mirrored in
  ``rebar.llm.code_review.registry._glob_match``). They are hoisted here so the
  deterministic close path can reuse ONE implementation without importing ``rebar.llm``;
  both former copies now delegate to :func:`glob_match`.

  (``rebar.grounding.oracle`` uses PLAIN ``fnmatch`` with no ``**/`` handling — it is a
  different rule, not a copy of these semantics, and is deliberately left alone.)
"""

from __future__ import annotations

import os
import subprocess
from fnmatch import fnmatch

# Paths a change may touch WITHOUT being declared in a ticket's ``file_impact``.
# Owned by the file-impact-vs-diff close check; deliberately NOT inherited from any
# other gate's exemption list, so tightening one never silently tightens the other.
EXEMPT_GLOBS: tuple[str, ...] = (
    "tests/**",
    "docs/**",
    "**/*.md",
    "CHANGELOG.md",
)


def glob_match(path: str, pattern: str) -> bool:
    """Match ``path`` against ``pattern``, with ``**/`` also matching at the repo root.

    ``fnmatch`` does not special-case ``/``, so a bare ``fnmatch("README.md", "**/*.md")``
    is False even though the intent is "any markdown file". Retrying with the leading
    ``**/`` stripped bridges that gap. This is the single implementation of the rule.
    """
    return fnmatch(path, pattern) or (pattern.startswith("**/") and fnmatch(path, pattern[3:]))


def normalize(path: str) -> str:
    """Normalize to a repo-relative POSIX path (no ``./`` prefix, no trailing ``/``)."""
    cleaned = path.strip().replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned.rstrip("/")


def is_exempt(path: str) -> bool:
    """Whether a changed path is covered by this check's own exemption globs."""
    normalized = normalize(path)
    return any(glob_match(normalized, pattern) for pattern in EXEMPT_GLOBS)


def _is_directory_entry(entry: str, repo_root: str | None) -> bool:
    """Whether a ``file_impact`` entry denotes a directory (trailing ``/`` or on disk)."""
    if entry.rstrip().endswith("/"):
        return True
    if repo_root is None:
        return False
    return os.path.isdir(os.path.join(repo_root, normalize(entry)))


def impact_covers(entry: str, changed: str, *, repo_root: str | None = None) -> bool:
    """Whether one ``file_impact`` entry covers a changed path.

    Exact equality, or a directory-prefix match when the entry ends with ``/`` or names an
    existing directory. NEVER a bare substring match — ``src/rebar/a.py`` must not cover
    ``src/rebar/a.py.bak``.
    """
    normalized_entry = normalize(entry)
    normalized_changed = normalize(changed)
    if not normalized_entry or not normalized_changed:
        return False
    if normalized_entry == normalized_changed:
        return True
    if _is_directory_entry(entry, repo_root):
        return normalized_changed.startswith(f"{normalized_entry}/")
    return False


def undeclared_paths(
    changed: list[str], impact: list[str], *, repo_root: str | None = None
) -> list[str]:
    """Changed paths that are neither covered by ``impact`` nor exempt (sorted, deduped)."""
    offending = {
        normalize(path)
        for path in changed
        if normalize(path)
        and not is_exempt(path)
        and not any(impact_covers(entry, path, repo_root=repo_root) for entry in impact)
    }
    return sorted(offending)


def is_merge_commit(sha: str, repo_root: str) -> bool:
    """Whether ``sha`` has more than one parent.

    Merges are skipped by the close check: ``git show --name-only`` renders a merge as a
    combined diff (usually an EMPTY path list), so reading it as "touched nothing" would
    silently pass. A merge authors no change of its own — its content arrives through the
    parents' own commits, which are scanned in their own right.
    """
    proc = subprocess.run(
        ["git", "-C", str(repo_root), "rev-list", "--parents", "-n", "1", sha],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return False
    return len(proc.stdout.split()) > 2


def changed_paths(sha: str, repo_root: str) -> list[str] | None:
    """Repo-relative paths ``sha`` touches, or ``None`` when git could not read it.

    ``None`` is distinct from ``[]`` on purpose: the caller fails closed on an unreadable
    commit it expected to be local, but treats a genuinely empty commit as clean.
    """
    proc = subprocess.run(
        ["git", "-C", str(repo_root), "show", "--name-only", "--pretty=format:", sha],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    return [normalize(line) for line in proc.stdout.splitlines() if line.strip()]


def invalid_commit_shas(shas: list[str], repo_root: str) -> list[str]:
    """Which of ``shas`` do NOT resolve to a commit object in ``repo_root``.

    Callers validate the WHOLE batch before recording anything, which is what makes
    attach-commits all-or-nothing: one bad SHA records no event at all.
    """
    invalid = []
    for sha in shas:
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "rev-parse",
                "--verify",
                "--quiet",
                f"{sha}^{{commit}}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            invalid.append(sha)
    return invalid


def referencing_commits(
    ticket_ids: set[str] | list[str], tracker: str, repo_root: str
) -> list[str] | None:
    """SHAs of commits referencing ANY of ``ticket_ids``, newest first.

    A commit references a ticket via a ``rebar-ticket:`` trailer or a leading ``<id>:``
    subject token. Each extracted candidate goes through the SAME shared resolver the
    commit-ticket gate uses, so every id form — full / short / alias / Jira key / prefix —
    matches. Resolves run ``quiet``: these are historical candidates the user never supplied,
    so an unrelated ambiguity is noise, not a diagnostic (bug af11); ambiguous candidates
    resolve to ``None`` either way, so the decision is unchanged. Resolves are cached.

    ``None`` is distinct from ``[]`` on purpose — the same distinction :func:`changed_paths`
    makes. ``[]`` means "history was read and nothing references these ids"; ``None`` means
    the history could not be read at all (not a repo, no commits yet). A caller that refuses
    on "no referencing commit" must not refuse on "this clone has no history", and the close
    gate — whose long-standing contract is a plain list — collapses ``None`` to ``[]``.
    """
    from rebar._commands.verify_commit import extract_ticket_refs
    from rebar._engine_support.resolver import build_resolver_scan_index, resolve_ticket_id

    accepted = set(ticket_ids)
    proc = subprocess.run(
        ["git", "-C", str(repo_root), "log", "--format=%H%x1f%B%x00"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    # Build the resolver index ONCE up front. Without it, every distinct alias
    # trailer drives a fresh full-store alias scan AND every short-id / ordinary
    # subject prefix / four-digit fragment drives a fresh full tracker-root
    # listing (O(unique_candidates x store)), which on a large store turns a
    # single close into tens of minutes. `None` (tracker unreadable) falls back
    # to per-call scanning, preserving behavior.
    scan_index = build_resolver_scan_index(tracker)
    alias_index = scan_index.alias_to_dirs if scan_index is not None else None
    dir_names = scan_index.sorted_dir_names if scan_index is not None else None
    resolved_cache: dict[str, str | None] = {}
    found: list[str] = []
    for entry in proc.stdout.split("\0"):
        sha, _, message = entry.partition("\x1f")
        sha = sha.strip()
        if not sha:
            continue
        for ref in extract_ticket_refs(message):
            if ref not in resolved_cache:
                resolved_cache[ref] = resolve_ticket_id(
                    ref, tracker, quiet=True, alias_index=alias_index, dir_names=dir_names
                )
            if resolved_cache[ref] in accepted:
                found.append(sha)
                break
    return found
