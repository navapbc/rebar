"""Eval solver — run a reviewer prompt against one dataset case (epic 6f2d / WS-EVAL).

The three reviewers under eval (completion-verifier, ticket-quality, spec-alignment)
are AGENTIC ops that read the rebar STORE (show_ticket / list_tickets) and the REPO
(file tools), so — unlike the plan-review finders, which take inline plan text — they
cannot be fed a bare string. This solver stands up a disposable, per-case rebar store
+ fixture repo, seeds the case's synthetic ticket(s)/epic(s) and any fixture `files`,
then runs the REAL op with an injected runner: a ``FakeRunner`` offline (tests), the
live runner in the eval CI. It uses each op's existing ``repo_root`` + ``runner`` seams
(no production-op changes), so the live agent's OWN show_ticket / file tools read the
same disposable store the deterministic context assembly does.
"""

from __future__ import annotations

import pathlib
import subprocess
import tempfile
from contextlib import contextmanager

import rebar
from rebar.llm.runner import Runner

__all__ = ["run_case", "case_store"]


def _git_init(d: str) -> None:
    subprocess.run(["git", "init", "-q", d], check=True)
    subprocess.run(["git", "-C", d, "config", "user.email", "eval@rebar.local"], check=True)
    subprocess.run(["git", "-C", d, "config", "user.name", "rebar-eval"], check=True)
    pathlib.Path(d, "README.md").write_text("eval fixture\n")
    subprocess.run(["git", "-C", d, "add", "-A"], check=True)
    subprocess.run(["git", "-C", d, "commit", "-q", "-m", "init"], check=True)


def _write_files(root: str, files: dict | None) -> None:
    if not files:
        return
    for rel, content in files.items():
        p = pathlib.Path(root, rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    subprocess.run(["git", "-C", root, "add", "-A"], check=True)
    subprocess.run(["git", "-C", root, "commit", "-q", "-m", "fixture"], check=True)


def _title(text: str, default: str) -> str:
    for line in (text or "").splitlines():
        s = line.strip()
        if s.lower().startswith("title:"):
            return s.split(":", 1)[1].strip() or default
    return default


@contextmanager
def case_store(case: dict):
    """Yield a ``repo_root`` for a disposable git + rebar store seeded from ``case``
    (its fixture ``files``); removed on exit."""
    with tempfile.TemporaryDirectory(prefix="rebar-eval-") as d:
        _git_init(d)
        rebar.init_repo(repo_root=d)
        _write_files(d, case.get("files"))
        yield d


def _criterion_id(prompt_id: str, repo_root: str | None) -> str | None:
    """Resolve ``prompt_id`` to a plan-review CRITERION id (bare id or ``plan-review-<id>``)
    if it names one in the effective registry, else ``None`` (a non-criterion reviewer id).
    This is what opens ``run_case`` beyond the closed 3-id set (story 55b8)."""
    from rebar.llm.plan_review import registry

    cid = prompt_id[len("plan-review-") :] if prompt_id.startswith("plan-review-") else prompt_id
    try:
        return cid if cid in registry.by_id(repo_root) else None
    except Exception:  # noqa: BLE001 — registry unresolvable ⇒ treat as non-criterion
        return None


def _run_criterion_case(cid: str, case: dict, *, runner: Runner, repo_root: str | None) -> dict:
    """Run ONE plan-review criterion as its Pass-1 finder over the case's inline ``input``
    text and return a finding-shaped verdict ``{"findings": [...]}``. This is the INLINE-TEXT
    path (no ``case_store`` / ticket scaffolding — the agentic store-reader ops need that, a
    single-criterion finder does not). Fire ⇔ non-empty findings (story 55b8)."""
    from rebar.llm.config import resolve_gate_config
    from rebar.llm.plan_review import passes, registry
    from rebar.llm.plan_review.pass1 import CONTAINER_CRITERIA

    desc = registry.by_id(repo_root).get(cid)
    if desc is None:  # pragma: no cover — guarded by _criterion_id
        raise ValueError(f"unknown criterion {cid!r}")
    # Container (G3/G4) + ISF finders need a parent/child graph or a session log, not inline
    # text — out of scope for the inline-fixture eval; fail with a clear message, never silently.
    if cid in CONTAINER_CRITERIA or cid == "ISF":
        raise ValueError(
            f"criterion {cid!r} is a container/ISF finder (needs a ticket graph / session log), "
            "not runnable over an inline eval fixture"
        )
    plan = case.get("input") or ""
    cfg = resolve_gate_config(repo_root)
    findings = passes.pass1_chunk(
        runner, cfg, plan=plan, chunk=[desc], agentic=registry.exec_tier(desc) == "AGENT"
    )
    return {"findings": list(findings)}


def _run_novelty_case(case: dict, *, runner: Runner, repo_root: str | None) -> dict:
    """Run the plan-review Pass-2 NOVELTY sub-call (child 150b) over ONE labeled
    {carryover, novel} case: the case's current ``finding`` is scored against its
    ``prior_finding`` through the SAME ``score_novelty`` kernel path the Pass-3 rising
    floor uses, mirroring the RunRequest wiring in ``plan_review._score_floor_novelty``.
    Inline text — no ``case_store`` / ticket scaffolding. Returns ``{"novelty": <float>}``,
    the shape the ``discriminates_novelty`` scorer reads (bug cuddlesome-titanous-seamonkey)."""
    from rebar.llm.config import resolve_gate_config
    from rebar.llm.plan_review import passes
    from rebar.llm.review_kernel.verify import score_novelty
    from rebar.llm.runner import RunRequest

    cfg = resolve_gate_config(repo_root)
    plan = case.get("plan") or case.get("input") or ""
    system = passes._resolve_system(passes.PASS_NOVELTY, plan, cfg)
    # ``criteria`` is required by the kernel finding listing (finding_listing); an eval
    # case carries none, so it is empty.
    findings: list[dict] = [{"finding": case.get("finding") or "", "criteria": []}]
    prior = [{"id": case.get("pair") or "prior-1", "finding": case.get("prior_finding") or ""}]

    def run_chunk(instructions: str, context: str) -> list[dict]:
        req = RunRequest(
            system_prompt=system,
            instructions=f"{instructions}\n\n## Prior-review findings (context)\n{context}",
            config=cfg,
            reviewers=["plan-novelty"],
            mode="structured",
            output_schema="plan_review_novelty",
            execution_mode="single_turn",
        )
        return runner.run(req).get("novelties", []) or []

    novelty_map = score_novelty(
        findings,
        prior_findings=prior,
        run_chunk=run_chunk,
        window_tokens=100_000,
        est_tokens=lambda s: len(s) // 4 + 1,
    )
    # One finding at index 0; score_novelty's fail-safe maps a failed/malformed sub-call
    # to 0.0 (carryover), so a broken live run surfaces as the novel cases failing.
    return {"novelty": novelty_map.get(0, 0.0)}


def _code_review_prompt_id(prompt_id: str) -> str | None:
    """Resolve ``prompt_id`` to a code-review PROMPT id this arm can eval as a single-prompt
    run over a case's diff, else ``None``. The evaluable set is the base reviewer
    (``code-review-base``), the Pass-2 verifier (``code-review-verify``), and the 11 specialist
    overlays (``code-review-<overlay>`` for overlay in ``code_review.registry.OVERLAY_IDS``) —
    the coach prompt is NOT evaluated. Guarded by the ``code-review-`` prefix so it can never
    collide with a plan-review criterion id (story f93a)."""
    if not prompt_id.startswith("code-review-"):
        return None
    from rebar.llm.code_review.registry import OVERLAY_IDS

    valid = {"code-review-base", "code-review-verify"} | {
        f"code-review-{oid}" for oid in OVERLAY_IDS
    }
    return prompt_id if prompt_id in valid else None


def _run_code_review_case(
    prompt_id: str, case: dict, *, runner: Runner, repo_root: str | None
) -> dict:
    """Run ONE code-review prompt over a case's inline ``diff`` and return the prompt's NATIVE
    structured output — a base/overlay reviewer's ``{findings:[...]}`` (+ base's
    ``recommend_overlays``) or the verifier's ``{verifications:[...]}``. This is per-prompt
    calibration granularity, NOT the full four-pass gate: the returned output is scored on
    findings presence (recall / no-fire) or verification presence, never a gate verdict
    (story f93a). Mirrors the RunRequest wiring in ``rebar.llm.workflow.runs``."""
    from rebar.llm.config import resolve_gate_config
    from rebar.llm.prompting import prompts
    from rebar.llm.runner import RunRequest

    prompt = prompts.get_prompt(prompt_id, repo_root=repo_root)
    diff = case.get("diff") or case.get("input") or ""
    variables = {"ticket_id": str(case.get("id", "")), "ticket_context": diff}
    cfg = resolve_gate_config(repo_root)
    system_prompt, instructions, langfuse_prompt = prompts.resolve_prompt_cached(
        prompt, variables, base_instructions="", langfuse_cfg=cfg.langfuse, repo_root=repo_root
    )
    req = RunRequest(
        system_prompt=system_prompt,
        instructions=instructions,
        config=cfg,
        reviewers=[prompt_id],
        langfuse_prompt=langfuse_prompt,
        mode="structured",
        output_schema=prompt.outputs if isinstance(prompt.outputs, str) else None,
        execution_mode=(prompt.execution_mode or "agentic"),
    )
    return runner.run(req)


def run_case(
    prompt_id: str, case: dict, *, runner: Runner, graph: bool = False, repo_root: str | None = None
) -> dict:
    """Run one reviewer/criterion over one dataset ``case``; return its structured output.

    ``runner`` is a ``FakeRunner`` offline or the config/live runner in CI. A ``prompt_id`` that
    names a plan-review criterion (built-in or activated project criterion, bare or
    ``plan-review-<id>``) runs as its Pass-1 finder over ``case['input']`` (no store scaffolding);
    ``plan-review-novelty`` runs the Pass-2 novelty sub-call over the case's
    ``finding``/``prior_finding`` pair; the three agentic reviewers keep their disposable-store
    path. ``repo_root`` (default ``config.repo_root()``) is the root the criterion
    registry/overlay resolves against. Raises ``ValueError`` for an unknown, non-criterion
    ``prompt_id``."""
    from rebar.llm import completion, operations, spec_scan

    if repo_root is None:
        from rebar import config as _config

        try:
            repo_root = str(_config.repo_root())
        except Exception:  # noqa: BLE001 — no repo ⇒ packaged criteria only
            repo_root = None

    # Novelty arm (bug cuddlesome-titanous-seamonkey): the Pass-2 novelty sub-call scores a
    # case's finding against its prior_finding — inline text, no disposable store. Checked by
    # EXACT id BEFORE the criterion arm, which would otherwise strip the prefix and misread
    # the id as a (nonexistent) criterion "novelty".
    from rebar.llm.plan_review import passes as _passes

    if prompt_id == _passes.PASS_NOVELTY:
        return _run_novelty_case(case, runner=runner, repo_root=repo_root)

    # Criterion arm (story 55b8): a plan-review criterion is a finder over inline text — no
    # disposable store. Checked before case_store so a criterion id never falls through.
    cid = _criterion_id(prompt_id, repo_root)
    if cid is not None:
        return _run_criterion_case(cid, case, runner=runner, repo_root=repo_root)

    # Code-review arm (story f93a): run ONE code-review prompt over the case's inline diff
    # and return its NATIVE structured output — no disposable store / ticket scaffolding
    # (like the criterion arm, the input is inline text). Checked before case_store so a
    # code-review id never falls through to the agentic reviewers.
    if _code_review_prompt_id(prompt_id) is not None:
        return _run_code_review_case(prompt_id, case, runner=runner, repo_root=repo_root)

    # The code-reading gates snapshot a ref (default origin/main) unless source=local;
    # the disposable fixture store has no origin, so always read its in-place checkout.
    src = "local"
    with case_store(case) as root:
        if prompt_id == "completion-verifier":
            desc = case.get("ticket_context") or ""
            tt = case.get("ticket_type") or ("bug" if "bug:" in desc.lower() else "task")
            tid = rebar.create_ticket(
                tt, _title(desc, "Eval ticket"), description=desc, repo_root=root, return_alias=True
            )["id"]
            return completion.verify_completion(
                tid, repo_root=root, runner=runner, graph=graph, source=src
            )
        if prompt_id == "ticket-quality":
            desc = case.get("ticket_context") or ""
            tt = case.get("ticket_type") or "task"
            tid = rebar.create_ticket(
                tt, _title(desc, "Eval ticket"), description=desc, repo_root=root, return_alias=True
            )["id"]
            return operations.review_ticket(
                tid, repo_root=root, runner=runner, graph=graph, source=src
            )
        if prompt_id == "spec-alignment":
            ids: list[str] = []
            for i, epic in enumerate(case.get("epics") or []):
                etext = epic if isinstance(epic, str) else str(epic)
                edesc = f"## Acceptance Criteria\n- [ ] {etext}\n\n## Success Criteria\n- {etext}"
                ids.append(
                    rebar.create_ticket(
                        "epic",
                        _title(etext, f"Epic {i + 1}"),
                        description=edesc,
                        repo_root=root,
                        return_alias=True,
                    )["id"]
                )
            return spec_scan.scan_epics_for_spec(
                case.get("spec") or "", epics=ids, repo_root=root, runner=runner, source=src
            )
        raise ValueError(f"no eval solver for prompt {prompt_id!r}")
