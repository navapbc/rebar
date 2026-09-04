"""Pre-fetch declared file_impact contents + referencing-commit diffs for the verifier.

The completion verifier used to receive only a fenced ticket_context (description + comments)
with ZERO file contents, so it re-discovered the ticket's declared files agentically — a burst
of expensive read_file/search_files round-trips before it could judge anything. This module
pre-loads the ticket's declared ``file_impact`` file CONTENTS (and, best-effort, the diffs of
the commits that reference the ticket) into a bounded, labelled
``<prefetched_file_contents>`` section that ``operations.assemble_context`` appends INSIDE the
untrusted ticket fence.

The section is *assistance, not a ceiling*: the verifier's read/search tools are unchanged, so
it can always fetch more or re-read anything. Everything here is deterministic (no timestamps),
bounded (an aggregate token budget plus a per-file char cap with skeleton compression for large
files), and fail-open (an unreadable path or a git hiccup is skipped, never fatal). An oversize
prefetch is TRIMMED to the model's physical context ceiling by :func:`fit_within_ceiling` at the
gate, rather than raising downstream.

The public surface splits IO from the pure assembler so the arithmetic is unit-pinnable:
:func:`rank_paths`, :func:`skeleton_compress`, :func:`clamp_and_format`,
:func:`test_glob_for_module`, and :func:`fit_within_ceiling` are pure; :func:`assemble_prefetch`
(and its :func:`referencing_commit_diffs` helper) do the disk/git reads.
"""

from __future__ import annotations

import glob as _glob
import os
import subprocess
from dataclasses import dataclass

__all__ = [
    "PREFETCH_PER_FILE_CHAR_CAP",
    "PREFETCH_TAG",
    "PREFETCH_TOKEN_BUDGET",
    "SKELETON_LINE_THRESHOLD",
    "PrefetchSpec",
    "assemble_prefetch",
    "clamp_and_format",
    "fit_within_ceiling",
    "rank_paths",
    "referencing_commit_diffs",
    "skeleton_compress",
    "test_glob_for_module",
]

#: Aggregate token cap for the WHOLE prefetch section (est. chars//4). Lowest-ranked files are
#: dropped once the running total would exceed this.
PREFETCH_TOKEN_BUDGET = 30000

#: Per-file body char cap before skeleton-compression is considered.
PREFETCH_PER_FILE_CHAR_CAP = 12000

#: Files with more than this many lines are skeleton-compressed before being included.
SKELETON_LINE_THRESHOLD = 500

#: The section label; the block is wrapped
#: ``<prefetched_file_contents>...</prefetched_file_contents>``.
PREFETCH_TAG = "prefetched_file_contents"

#: Cap on referencing commits included, and the char cap per commit diff (deterministic + cheap).
_MAX_REFERENCING_COMMITS = 3
_COMMIT_DIFF_CHAR_CAP = 4000


@dataclass(frozen=True)
class PrefetchSpec:
    """Pure config for one prefetch assembly (``repo_root`` is passed to the assembler)."""

    ticket_id: str
    graph: bool
    token_budget: int = PREFETCH_TOKEN_BUDGET
    per_file_char_cap: int = PREFETCH_PER_FILE_CHAR_CAP


def rank_paths(own: list[str], subtree_freq: dict[str, int], *, graph: bool) -> list[str]:
    """Deterministic path ranking.

    On the CLOSE path (``graph=False``) return ONLY ``own`` (dedup, first-seen order); the
    subtree frequency map is ignored. On ``graph=True``: ``own`` paths FIRST (given order),
    then the remaining subtree paths by DESCENDING frequency (ties broken by path ascending),
    excluding any already in ``own``. No path appears twice.
    """
    ranked: list[str] = []
    seen: set[str] = set()
    for path in own:
        if path not in seen:
            seen.add(path)
            ranked.append(path)
    if not graph:
        return ranked
    remaining = [(p, f) for p, f in subtree_freq.items() if p not in seen]
    remaining.sort(key=lambda pf: (-pf[1], pf[0]))
    for path, _freq in remaining:
        if path not in seen:
            seen.add(path)
            ranked.append(path)
    return ranked


def _is_signature_line(stripped: str) -> bool:
    return (
        stripped.startswith("def ")
        or stripped.startswith("async def ")
        or stripped.startswith("class ")
        or stripped.startswith("@")
    )


def skeleton_compress(text: str) -> str:
    """Skeletonize an oversize Python body: keep signature/decorator lines (and the first
    docstring line after a def/class), replace each elided run with a single marker line
    naming ``read_file``. Line-based and deterministic.
    """
    lines = text.splitlines()
    kept: list[str] = []
    elided = 0
    prev_was_sig = False

    def flush_elided() -> None:
        nonlocal elided
        if elided:
            kept.append(
                f"    ... <{elided} lines elided; call read_file to fetch the full body> ..."
            )
            elided = 0

    for line in lines:
        stripped = line.strip()
        is_sig = _is_signature_line(stripped)
        is_docstring = prev_was_sig and (
            stripped.startswith('"""')
            or stripped.startswith("'''")
            or stripped.startswith('r"""')
            or stripped.startswith("r'''")
        )
        if is_sig or is_docstring:
            flush_elided()
            kept.append(line)
            prev_was_sig = is_sig
        else:
            elided += 1
            prev_was_sig = False
    flush_elided()
    return "\n".join(kept)


def _render_block(path: str, body: str, mode: str) -> str:
    return f"--- {path} ({mode}) ---\n{body}"


def _estimate_tokens(rendered: str) -> int:
    return max(1, len(rendered) // 4)


def clamp_and_format(
    ranked: list[str],
    bodies: dict[str, str],
    *,
    token_budget: int,
    per_file_char_cap: int,
) -> tuple[str, list[dict]]:
    """Pure assembler. Walk ``ranked`` in order; a path absent from ``bodies`` is skipped. For
    each present path: if its body exceeds ``per_file_char_cap`` OR its line count exceeds
    :data:`SKELETON_LINE_THRESHOLD`, use :func:`skeleton_compress` (mode ``"skeleton"``), else
    mode ``"full"``. Estimate tokens as ``max(1, len(rendered)//4)`` and STOP adding once the
    running total would exceed ``token_budget`` (lowest-ranked dropped, never added).

    Returns ``(section_text, manifest)``: ``manifest`` lists only INCLUDED files in order as
    ``{"path", "mode"}``; ``section_text`` wraps a compact ``PRE-LOAD MANIFEST:`` block plus the
    per-file blocks in ``<prefetched_file_contents>...</prefetched_file_contents>``.
    """
    included: list[tuple[str, str, str]] = []  # (path, mode, rendered_block)
    manifest: list[dict] = []
    running = 0
    for path in ranked:
        if path not in bodies:
            continue
        body = bodies[path]
        if len(body) > per_file_char_cap or body.count("\n") + 1 > SKELETON_LINE_THRESHOLD:
            body = skeleton_compress(body)
            mode = "skeleton"
        else:
            mode = "full"
        block = _render_block(path, body, mode)
        cost = _estimate_tokens(block)
        if running + cost > token_budget:
            break
        running += cost
        included.append((path, mode, block))
        manifest.append({"path": path, "mode": mode})

    manifest_lines = ["PRE-LOAD MANIFEST:"]
    manifest_lines.extend(f"- {path}: {mode}" for path, mode, _block in included)
    manifest_lines.append("")
    inner = "\n".join(manifest_lines) + "\n" + "\n".join(block for _p, _m, block in included)
    section_text = f"<{PREFETCH_TAG}>\n{inner}\n</{PREFETCH_TAG}>"
    return section_text, manifest


def test_glob_for_module(path: str) -> str:
    """``src/rebar/llm/foo.py`` -> ``tests/**/test_foo*.py``; a non-``.py`` path -> ``""``."""
    if not path.endswith(".py"):
        return ""
    stem = os.path.basename(path)[: -len(".py")]
    return f"tests/**/test_{stem}*.py"


def fit_within_ceiling(base_context: str, prefetch_section: str, model: str | None) -> str:
    """Trim ``prefetch_section`` so ``len(base_context) + len(returned) <= ceiling`` for
    ``model``. Returns unchanged if it already fits; ``""`` if ``base_context`` alone already
    fills the ceiling. Otherwise trims whole trailing file blocks first, else hard-truncates,
    and appends a truncation marker. NEVER returns something that overflows the ceiling.
    """
    from rebar.llm.workflow.completion_recovery import physical_context_ceiling

    ceiling = physical_context_ceiling(model)
    budget = ceiling - len(base_context)
    if budget <= 0:
        return ""
    if len(prefetch_section) <= budget:
        return prefetch_section

    marker = "\n... <prefetch truncated to fit context ceiling> ..."
    allowance = budget - len(marker)
    if allowance <= 0:
        return prefetch_section[:budget]

    # Prefer cutting whole trailing file blocks (each begins with a "--- path (mode) ---" header).
    header = "\n--- "
    cut = prefetch_section[:allowance]
    boundary = cut.rfind(header)
    if boundary > 0:
        trimmed = prefetch_section[:boundary]
    else:
        trimmed = prefetch_section[:allowance]
    result = trimmed + marker
    if len(result) > budget:  # defensive: never overflow
        result = result[:budget]
    return result


def _normalize_impact(file_impact) -> list[str]:
    """Coerce a ticket's ``file_impact`` (dicts with ``path`` or bare strings) to a path list."""
    paths: list[str] = []
    for entry in file_impact or []:
        if isinstance(entry, str):
            path = entry
        elif isinstance(entry, dict):
            path = entry.get("path") or ""
        else:
            path = ""
        if path:
            paths.append(path)
    return paths


def _subtree_impacts(ticket_id: str, repo_root) -> dict[str, int]:
    """BFS the subtree (mirroring operations.assemble_context) and count how many descendants
    declare each path. The root's own file_impact is NOT counted here."""
    from collections import deque

    from rebar import _reads

    freq: dict[str, int] = {}
    seen = {ticket_id}
    frontier = deque([ticket_id])
    while frontier:
        parent = frontier.popleft()
        try:
            children = _reads.list_tickets(parent=parent, repo_root=repo_root)
        except Exception:  # noqa: BLE001 — fail-open on a read error
            continue
        for child in sorted(children, key=lambda c: c.get("ticket_id", "")):
            cid = child.get("ticket_id")
            if not cid or cid in seen:
                continue
            seen.add(cid)
            frontier.append(cid)
            for path in _normalize_impact(child.get("file_impact")):
                freq[path] = freq.get(path, 0) + 1
    return freq


def referencing_commit_diffs(ticket_id: str, repo_root) -> dict[str, str]:
    """Best-effort ``{sha: diff_text}`` for up to :data:`_MAX_REFERENCING_COMMITS` recent
    commits reachable from HEAD that reference ``ticket_id`` (a ``rebar-ticket:`` trailer or a
    leading ``<id>:`` subject). Trivially monkeypatchable; returns ``{}`` on ANY error."""
    try:
        grep_args = [
            "git",
            "-C",
            str(repo_root),
            "log",
            "-P",
            f"--grep=(^|\\b){ticket_id}\\b",
            f"--max-count={_MAX_REFERENCING_COMMITS}",
            "--format=%H",
            "HEAD",
        ]
        proc = subprocess.run(grep_args, capture_output=True, text=True, timeout=10, check=False)
        if proc.returncode != 0:
            return {}
        shas = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
        diffs: dict[str, str] = {}
        for sha in shas[:_MAX_REFERENCING_COMMITS]:
            show = subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo_root),
                    "show",
                    "--stat",
                    "--format=%H%n%s%n",
                    sha,
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if show.returncode != 0:
                continue
            diffs[sha] = show.stdout[:_COMMIT_DIFF_CHAR_CAP]
        return diffs
    except Exception:  # noqa: BLE001 — git integration is best-effort, fail-open
        return {}


def _read_body(repo_root, path: str) -> str | None:
    try:
        full = os.path.join(str(repo_root), path)
        with open(full, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except Exception:  # noqa: BLE001 — unreadable/missing path is skipped, not fatal
        return None


def _discover_test_globs(repo_root, ranked: list[str], already: set[str]) -> list[str]:
    """Best-effort matching test files for ranked source modules, appended (deterministic,
    sorted) at the END of the ranked list. Fail-open."""
    extra: list[str] = []
    seen: set[str] = set()
    for path in ranked:
        pattern = test_glob_for_module(path)
        if not pattern:
            continue
        try:
            matches = _glob.glob(os.path.join(str(repo_root), pattern), recursive=True)
        except Exception:  # noqa: BLE001
            continue
        for match in sorted(matches):
            rel = os.path.relpath(match, str(repo_root))
            if rel not in already and rel not in seen:
                seen.add(rel)
                extra.append(rel)
    return extra


def assemble_prefetch(spec: PrefetchSpec, *, repo_root) -> tuple[str, list[dict]]:
    """IO orchestrator: load the ticket's declared file_impact, rank + read the bodies, gather
    referencing-commit diffs (fail-open), and return :func:`clamp_and_format`'s
    ``(section_text, manifest)``. An empty result (no readable paths, no diffs) yields an empty
    section string so the caller appends nothing."""
    from rebar import _reads
    from rebar.llm.gate_context import resolve_code_root

    try:
        ticket = _reads.show_ticket(spec.ticket_id, repo_root=repo_root)
    except Exception:  # noqa: BLE001 — a missing/unreadable ticket yields no prefetch
        return "", []
    own = _normalize_impact(ticket.get("file_impact"))
    canonical = ticket.get("ticket_id", spec.ticket_id)

    # Verdict-bearing WORKING-TREE reads (file bodies + test-glob discovery) MUST resolve to the
    # `--ref`-pinned code snapshot the attested gate activated (`current_code_root()` via
    # `use_code_root(handle.path)`), NOT the live `repo_root` — which is the ticket-store / server
    # checkout the workflow threads through. Bug 831f (shimmery-customary-dorking): reading these
    # from `repo_root` leaked the live checkout's (B's) bytes into the verifier's
    # `<prefetched_file_contents>` evidence on a `--ref A` run. `allow_checkout_fallback=False`
    # yields the snapshot-or-`None`, so local/no-gate mode (no active snapshot) preserves the prior
    # behaviour by falling back to the passed `repo_root`. TICKET reads (`show_ticket`,
    # `_subtree_impacts`) and immutable-by-sha referencing-commit diffs stay on `repo_root` (the
    # materialized snapshot has no `.git`, and a commit diff is fixed by its SHA, not live state).
    code_root = resolve_code_root(allow_checkout_fallback=False) or repo_root

    if spec.graph:
        subtree_freq = _subtree_impacts(canonical, repo_root)
    else:
        subtree_freq = {}

    ranked = rank_paths(own, subtree_freq, graph=spec.graph)
    ranked = ranked + _discover_test_globs(code_root, ranked, set(ranked))

    bodies: dict[str, str] = {}
    for path in ranked:
        body = _read_body(code_root, path)
        if body is not None:
            bodies[path] = body

    # Referencing-commit diffs count against the SAME budget, AFTER the file bodies.
    diffs = referencing_commit_diffs(canonical, repo_root)
    for sha, diff_text in diffs.items():
        pseudo = f"referencing-commit {sha}"
        ranked.append(pseudo)
        bodies[pseudo] = diff_text

    if not bodies:
        return "", []

    return clamp_and_format(
        ranked,
        bodies,
        token_budget=spec.token_budget,
        per_file_char_cap=spec.per_file_char_cap,
    )
