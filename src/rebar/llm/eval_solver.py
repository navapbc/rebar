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


def run_case(prompt_id: str, case: dict, *, runner: Runner, graph: bool = False) -> dict:
    """Run one reviewer over one dataset ``case``; return its structured output.

    ``runner`` is a ``FakeRunner`` offline or the config/live runner in CI. Raises
    ``ValueError`` for an unknown ``prompt_id``."""
    from rebar.llm import completion, operations, spec_scan

    with case_store(case) as root:
        if prompt_id == "completion-verifier":
            desc = case.get("ticket_context") or ""
            tt = case.get("ticket_type") or ("bug" if "bug:" in desc.lower() else "task")
            tid = rebar.create_ticket(
                tt, _title(desc, "Eval ticket"), description=desc, repo_root=root, return_alias=True
            )["id"]
            return completion.verify_completion(tid, repo_root=root, runner=runner, graph=graph)
        if prompt_id == "ticket-quality":
            desc = case.get("ticket_context") or ""
            tt = case.get("ticket_type") or "task"
            tid = rebar.create_ticket(
                tt, _title(desc, "Eval ticket"), description=desc, repo_root=root, return_alias=True
            )["id"]
            return operations.review_ticket(tid, repo_root=root, runner=runner, graph=graph)
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
                case.get("spec") or "", epics=ids, repo_root=root, runner=runner
            )
        raise ValueError(f"no eval solver for prompt {prompt_id!r}")
