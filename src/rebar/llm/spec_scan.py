"""Batch spec-scan operation: evaluate open epics against a specification.

Shaped as a **batch evaluator** rather than one giant agent loop: candidate epics
are chunked into batches, each batch is evaluated against the spec in its own
runner pass (bounded cost), and the per-batch findings are concatenated and ranked
by severity. Reuses the findings contract + runner seam like every other op.
"""

from __future__ import annotations

from collections.abc import Iterator

from rebar.llm import prompts
from rebar.llm.config import LLMConfig
from rebar.llm.findings import build_result, resolve_citations, validate_result
from rebar.llm.runner import Runner, RunRequest, get_runner

__all__ = ["scan_epics_for_spec"]

_SEVERITY_RANK = {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}
_DESC_CAP = 600  # per-epic description chars inlined into the batch context


def _fetch_epics(epics: list[str] | None, repo_root) -> list[dict]:
    import rebar

    if epics:
        return [rebar.show_ticket(e, repo_root=repo_root) for e in epics]
    return rebar.list_tickets(ticket_type="epic", status="open,in_progress", repo_root=repo_root)


def _render_epic(t: dict) -> str:
    desc = (t.get("description") or "").strip()
    if len(desc) > _DESC_CAP:
        desc = desc[:_DESC_CAP] + "…"
    return f"### Epic {t.get('ticket_id', '?')} — {t.get('title', '')}\n{desc}"


def _chunks(seq: list, size: int) -> Iterator[list]:
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def scan_epics_for_spec(
    spec_text: str,
    *,
    epics: list[str] | None = None,
    batch_size: int = 5,
    reviewer_id: str = "spec-alignment",
    repo_root=None,
    config: LLMConfig | None = None,
    runner: Runner | None = None,
) -> dict:
    """Scan open epics against ``spec_text`` for coverage gaps, conflicts, and
    overlaps; return a ``review_result``.

    Epics default to the store's open/in-progress epics (or pass explicit ids).
    They are evaluated in batches of ``batch_size`` (one runner pass each), and the
    findings are concatenated + ranked by severity.
    """
    cfg = config or LLMConfig.from_env(repo_root=repo_root)
    tickets = _fetch_epics(epics, repo_root)
    reviewer = prompts.get_reviewer(reviewer_id)
    selected = get_runner(cfg, override=runner)
    # Probe runner readiness up front (import-only, no model call) so a missing
    # ``agents`` extra (or a misconfigured runner) degrades cleanly even when there
    # are zero epics to scan — otherwise the batch loop below never runs and an
    # unusable runner would masquerade as an empty-but-successful result.
    selected.preflight()

    epic_ids = [t.get("ticket_id") for t in tickets]
    findings: list[dict] = []
    batches = 0
    for batch in _chunks(tickets, max(1, batch_size)):
        batches += 1
        variables = {
            "spec": spec_text,
            "epics": "\n\n".join(_render_epic(t) for t in batch),
            "repo_path": cfg.repo_path or "",
        }
        system_prompt, lf_prompt = prompts.resolve_prompt(reviewer, variables, cfg.langfuse)
        instructions = (
            "Evaluate each epic in this batch against the spec: flag coverage gaps "
            "(spec points no epic covers), conflicts/contradictions, and scope "
            "overlaps. Cite the epic id with a source citation in every finding, and "
            "use your file tools for any code claim. Return findings via the "
            "structured output."
        )
        req = RunRequest(
            system_prompt=system_prompt,
            instructions=instructions,
            config=cfg,
            reviewers=[reviewer_id],
            target={"kind": "spec_scan", "ticket_ids": [t.get("ticket_id") for t in batch]},
            langfuse_prompt=lf_prompt,
        )
        findings.extend(selected.run(req).get("findings", []))

    findings.sort(
        key=lambda f: _SEVERITY_RANK.get(str(f.get("severity", "info")).lower(), 0), reverse=True
    )
    result = build_result(
        findings,
        runner=getattr(selected, "name", cfg.runner),
        model=cfg.model,
        trace_id=None,
        target={"kind": "spec_scan", "ticket_ids": epic_ids},
        reviewers=[reviewer_id],
        summary=f"scanned {len(tickets)} epic(s) in {batches} batch(es); "
        f"{len(findings)} finding(s).",
    )
    resolve_citations(result, cfg.repo_path)
    return validate_result(result)
