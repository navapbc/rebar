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


def run_case(
    prompt_id: str, case: dict, *, runner: Runner, graph: bool = False, repo_root: str | None = None
) -> dict:
    """Run one reviewer/criterion over one dataset ``case``; return its structured output.

    ``runner`` is a ``FakeRunner`` offline or the config/live runner in CI. A ``prompt_id`` that
    names a plan-review criterion (built-in or activated project criterion, bare or
    ``plan-review-<id>``) runs as its Pass-1 finder over ``case['input']`` (no store scaffolding);
    the three agentic reviewers keep their disposable-store path. ``repo_root`` (default
    ``config.repo_root()``) is the root the criterion registry/overlay resolves against. Raises
    ``ValueError`` for an unknown, non-criterion ``prompt_id``."""
    from rebar.llm import completion, operations, spec_scan

    # Criterion arm (story 55b8): a plan-review criterion is a finder over inline text — no
    # disposable store. Checked FIRST so a criterion id never falls through to case_store.
    if repo_root is None:
        from rebar import config as _config

        try:
            repo_root = str(_config.repo_root())
        except Exception:  # noqa: BLE001 — no repo ⇒ packaged criteria only
            repo_root = None
    cid = _criterion_id(prompt_id, repo_root)
    if cid is not None:
        return _run_criterion_case(cid, case, runner=runner, repo_root=repo_root)

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
