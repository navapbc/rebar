"""Agentic, in-CI investigator for the live Jira DC rich-text reconcile defect.

**This is an AUTHORING / DIAGNOSTIC tool, not a test-path component.** It is the debug
analog of ``scripts/jira_dc_capability_map.py`` (ticket 259b): where that script runs an
Opus agent over raw REST to *map* the pinned DC image, this module runs an Opus agent over
rebar's OWN reconcile primitives to *root-cause* a defect against a live, ephemeral Jira DC.

Why it exists: the working amd64 Jira DC only comes up inside the CI runner (arm64 hosts
stall under emulation — see ``README.md``). Every blind ``print``-and-push observation
therefore costs a full ~2h CI cycle. This module pays the DC boot cost ONCE, then hands an
agent a fast local investigation loop against the already-bound store + live instance: it
can compute the exact wire with ``WikiTextCodec``, diff with ``compute_update_fields``, run
a full scoped reconcile subprocess, write directly through the transport, and read the raw +
rendered ``description`` back — many experiments per boot instead of one per CI cycle.

The bound scenario is NOT re-ported here: this test depends on the SAME proven fixtures the
rest of the live suite uses (``bound_dc_issue`` → ``dc_store_copy_repo`` + a seeded, bound DC
issue), so the agent starts from the exact ``(repo, local_id, key)`` the failing rich-text
tests exercise, with the ``dc`` rich-text cutover on.

Guardrails mirror the capability-map run: OPT-IN ONLY (``REBAR_DC_RICHTEXT_PROBE=1``, set by a
``workflow_dispatch`` job — never a push/PR/schedule and never a normal external run), the DC
is a throwaway ephemeral instance, ``REBAR_MCP_READONLY=1`` keeps rebar's own ticket store
read-only, and the agent is instructed to REPORT the mechanism, never to fix the harness, the
repository, or rebar's configuration. It asserts only that the agent produced a structured
finding — it does NOT fail on discovering the bug (that is the point of the run); the finding
+ full experiment evidence are printed and written as an artifact for a human to act on.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import traceback
import uuid
from pathlib import Path
from typing import Any

import pytest
from _dc_support import live_jira_ready, source_repo_root
from _dc_support import skip_no_extra as _skip_no_extra
from _dc_support import skip_no_harness as _skip

# Not marked with the module-level ``_live_jira_ready`` the all-skip canary keys on: this is
# an opt-in diagnostic that is EXPECTED to skip on every ordinary run, so it must not count
# as a live test that was "collected but never executed".
_ = live_jira_ready

CONTRACT_NAME = "jira_dc_richtext_rca"

# Every raw REST call the agent's ``jira_rest`` tool makes and every ``run_python`` snippet it
# executes, captured with a stable id so the final finding is traceable to evidence rather
# than to a model summary (the same discipline scripts/jira_dc_capability_map.py enforces).
_EVIDENCE: list[dict[str, Any]] = []

_OPT_IN = os.environ.get("REBAR_DC_RICHTEXT_PROBE") == "1"
_HAVE_KEY = bool(
    os.environ.get("ANTHROPIC_API_KEY")
    or os.environ.get("OPENAI_API_KEY")
    or os.environ.get("AWS_ROLE_ARN")
    or os.environ.get("REBAR_LLM_MODEL")
)


def _build_contract() -> type:
    """The structured RCA the agent must return (rebar.llm.contracts seam)."""
    from pydantic import BaseModel, Field

    class Experiment(BaseModel):
        description: str = Field(description="What this experiment did, in one line.")
        evidence_ids: list[str] = Field(
            default_factory=list,
            description="The [ev-NNNN] ids of the run_python/jira_rest calls that back it.",
        )
        observation: str = Field(description="The decisive value(s) observed.")

    class RichTextRca(BaseModel):
        mechanism: str = Field(
            description=(
                "The proven mechanism: WHY the live reconcile stores description=None and "
                "re-emits every pass. Terminate at a specific changeable artifact "
                "(module:function/line), not a category."
            )
        )
        failing_layer: str = Field(
            description=(
                "Which layer produces the defect, one of: store_read | codec | differ | "
                "transport | seed | cutover_config | other."
            )
        )
        wire_computed_nonempty: bool = Field(
            description="Did the codec/compute path yield a NON-empty wire for the rich body?"
        )
        raw_stored_after_write: str | None = Field(
            default=None,
            description="The raw `description` read back after a write (None / empty / the wire).",
        )
        reemit_cause: str = Field(
            description="Why every subsequent pass re-emits changed=[description] (differ input)."
        )
        recommended_fix_site: str = Field(
            description="Where a fix would go (module:symbol), described — NOT applied."
        )
        attribution_hint: str = Field(
            default="",
            description="git blame / ticket lead for the originating change, if identified.",
        )
        experiments: list[Experiment] = Field(
            description="The discriminating experiments run, each cited to evidence ids."
        )
        confidence: str = Field(description="high | medium | low, with a one-line why.")

    return RichTextRca


def _register_contract_once() -> None:
    from rebar.llm.contracts import register_contract

    with contextlib.suppress(Exception):
        register_contract(CONTRACT_NAME, _build_contract)


_SYSTEM_PROMPT = """\
You are a senior debugging engineer investigating a rebar reconciler defect against a LIVE,
throwaway Jira Data Center instance inside a CI job. You apply the scientific method:
falsifiable hypotheses, discriminating experiments, runtime evidence over static reading.

YOUR JOB IS TO PROVE THE MECHANISM — you do NOT fix anything. Do not edit the repository, the
harness, or rebar's configuration. The Jira DC instance and the local ticket store are
ephemeral and were created solely for this run; you may freely create/edit/read issues and
run reconcile passes against them as your experiments require.

You have two tools:
  * run_python(code): executes Python in a PERSISTENT namespace that already holds warm
    handles (listed in the task). Use print() to observe; the tool returns captured
    stdout+stderr and each call gets an [ev-NNNN] evidence id. Build state across calls like a
    REPL. This is your primary instrument — call the reconciler primitives directly.
  * jira_rest(method, path, body): one raw authenticated REST call against the instance
    (e.g. GET /rest/api/2/issue/KEY?expand=renderedFields). Use it for ground-truth reads of
    what Jira actually stored, independent of any rebar code path.

Cite the [ev-NNNN] ids that back each experiment in your final structured answer. An answer a
reader cannot trace to an evidence id is a guess and defeats the run. Budget is finite — do
not repeat identical calls; each experiment should split the live hypotheses.
"""


def _instructions(local_id: str, key: str) -> str:
    return f"""\
THE DEFECT (observed live, never reproduced offline):
When the DC rich-text cutover is ON (REBAR_RECONCILER_RICH_TEXT_CUTOVER=dc, already set), a
reconcile pass that pushes a rich Markdown `description` to a bound DC issue emits
`RECON: outbound_update key=... changed=[description]` (it INTENDS to write) yet the DC then
stores `description = None`, and EVERY subsequent pass re-emits `changed=[description]` — it
never converges.

WHAT IS ALREADY RULED OUT OFFLINE (do not re-litigate; CONFIRM or REFUTE them live instead):
  * pandoc IS installed; WikiTextCodec(rich=True) renders the body to correct NON-empty wiki
    wire (h1./{{code}}) in isolation.
  * compute_update_fields CONVERGES (returns changed=[]) offline once the wire is the remote
    value.
  * the renderer never returns empty; JiraDataCenterTransport.update_issue does
    issue.update(fields={{"description": <value>}}) — byte-identical to a probe that PASSES.
So the defect manifests ONLY in the live reconcile path. Find the ONE layer where it diverges.

WARM NAMESPACE (already defined in run_python; do not re-import unless you want to):
  repo        -> Path to the bound store copy (rebar repo_root)
  local_id    -> {local_id!r}  (the bound local ticket id)
  key         -> {key!r}       (the bound DC issue key)
  project, base_url
  transport   -> the live JiraDataCenterTransport (has get_issue / update_issue)
  dc_request  -> harness raw REST helper: dc_request(path, method="GET", payload=None) -> tuple
  rebar       -> the rebar library (edit_ticket, show_ticket, ...)
  dc          -> module _dc_support (run_bridge, run_reconcile, seed_searchable_issue, ...)
  compute_update_fields, diff_canonical_fields  (rebar_reconciler.outbound_field_diff)
  WikiTextCodec, cutover_clients                (rebar_reconciler.adapters.jira_family.rich_text)
  rich_markdown(heading, bold, code) -> a representative rich body (h1 + bold + {{code}} macro)
  json, os, subprocess, uuid, textwrap

SUGGESTED INVESTIGATION (adapt as evidence dictates — the goal is the mechanism, not the steps):
  1. Confirm the cutover really is `dc` INSIDE this process AND inside a reconcile subprocess
     (cutover_clients()); a config read that fails closed to empty would silently disable rich.
  2. Set a rich description locally (rebar.edit_ticket(local_id, repo_root=repo,
     description=rich_markdown(...))) and read the LOCAL ticket back — is the stored local
     description intact, or already mangled?
  3. Compute what the reconciler WOULD send: call compute_update_fields / diff_canonical_fields
     with the real local+remote state and inspect the exact `description` wire bytes.
  4. Run the real scoped writing pass: dc.run_bridge(repo, "sync", only=f"{{local_id}},{{key}}",
     max_changes=10); read cp.stderr/stdout. Then read the raw stored description back with
     jira_rest GET /rest/api/2/issue/{key} and the rendered one with ?expand=renderedFields.
  5. Discriminate: does the subprocess (a) read a mangled LOCAL description, (b) compute an
     empty/wrong wire, or (c) SEND a value that Jira clears to None? Write the SAME computed
     wire DIRECTLY via transport.update_issue and read back — if that stores fine but the
     reconcile subprocess does not, the divergence is in the subprocess pipeline, not the codec
     or transport. Narrow until one changeable artifact remains.
  6. Explain the re-emit: why does the next pass still see changed=[description]? (What does the
     differ compare — the stored None vs the local body?)

Return the RichTextRca structured answer: the proven mechanism (module:symbol), the failing
layer, whether the wire was non-empty, what was stored, the re-emit cause, the recommended fix
site (described, NOT applied), and every experiment cited to evidence ids.
"""


def _make_tools(namespace: dict[str, Any], dc_request: Any):
    def run_python(code: str) -> str:
        """Execute Python in the persistent investigation namespace and return captured output.

        `code` runs in a namespace pre-loaded with warm handles (repo, local_id, key,
        transport, dc_request, the reconciler primitives — see the task). Use print() to
        observe values. State persists across calls, so build up an experiment step by step.
        Returns captured stdout+stderr (truncated for context) tagged with an evidence id.
        """
        ev_id = f"ev-{len(_EVIDENCE):04d}"
        buf = io.StringIO()
        error: str | None = None
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            try:
                exec(code, namespace)
            except Exception:  # noqa: BLE001 - surface any failure to the agent verbatim
                error = traceback.format_exc()
        out = buf.getvalue()
        if error:
            out = f"{out}\n{error}"
        _EVIDENCE.append({"id": ev_id, "kind": "run_python", "code": code, "output": out})
        rendered = out if len(out) <= 6000 else out[:6000] + "\n…(truncated; see evidence artifact)"
        return f"[{ev_id}] run_python ->\n{rendered}"

    def jira_rest(method: str, path: str, body: dict | None = None) -> str:
        """Make ONE raw authenticated REST call against the live Jira DC harness.

        `method` is GET/POST/PUT/DELETE; `path` is a REST path such as
        "/rest/api/2/issue/ABC-1?expand=renderedFields" (not a full URL). `body` is an
        optional JSON payload. Returns an evidence id, HTTP status, and the (possibly
        truncated) response — the ground-truth of what Jira stored, independent of rebar.
        """
        status, parsed = dc_request(path, method=method.upper(), payload=body)
        ev_id = f"ev-{len(_EVIDENCE):04d}"
        _EVIDENCE.append(
            {
                "id": ev_id,
                "kind": "jira_rest",
                "method": method.upper(),
                "path": path,
                "request_body": body,
                "status": status,
                "response_body": parsed,
            }
        )
        rendered = json.dumps(parsed, default=str)
        if len(rendered) > 4000:
            rendered = rendered[:4000] + " …(truncated; see evidence artifact)"
        return f"[{ev_id}] {method.upper()} {path} -> {status}\n{rendered}"

    return run_python, jira_rest


def _rich_markdown(heading: str, bold: str, code: str) -> str:
    return f"# {heading}\n\nA paragraph with **{bold}** emphasis.\n\n{{code}}\n{code}\n{{code}}\n"


def _build_namespace(
    repo: Path,
    local_id: str,
    key: str,
    project: str,
    base_url: str,
    transport: Any,
    dc_request: Any,
) -> dict[str, Any]:
    import subprocess
    import textwrap

    import _dc_support as dc
    from rebar_reconciler.adapters.jira_family.rich_text import (
        WikiTextCodec,
        cutover_clients,
    )
    from rebar_reconciler.outbound_field_diff import (
        compute_update_fields,
        diff_canonical_fields,
    )

    import rebar

    return {
        "repo": repo,
        "local_id": local_id,
        "key": key,
        "project": project,
        "base_url": base_url,
        "transport": transport,
        "dc_request": dc_request,
        "rebar": rebar,
        "dc": dc,
        "compute_update_fields": compute_update_fields,
        "diff_canonical_fields": diff_canonical_fields,
        "WikiTextCodec": WikiTextCodec,
        "cutover_clients": cutover_clients,
        "rich_markdown": _rich_markdown,
        "json": json,
        "os": os,
        "subprocess": subprocess,
        "uuid": uuid,
        "textwrap": textwrap,
    }


def _write_artifacts(result: dict[str, Any] | None) -> Path:
    out_dir = Path(
        os.environ.get("JIRA_DC_PROBE_OUTPUT_DIR")
        or (source_repo_root() / "jira-dc-richtext-probe")
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "evidence.json").write_text(json.dumps(_EVIDENCE, indent=2, default=str))
    if result is not None:
        (out_dir / "rca.json").write_text(json.dumps(result, indent=2, default=str))
    return out_dir


@_skip
@_skip_no_extra
@pytest.mark.skipif(
    not _OPT_IN,
    reason="opt-in agentic probe: set REBAR_DC_RICHTEXT_PROBE=1 (workflow_dispatch only)",
)
@pytest.mark.skipif(
    not _HAVE_KEY,
    reason="no LLM credential (ANTHROPIC_API_KEY / OPENAI_API_KEY / bedrock role) in env",
)
def test_agentic_richtext_investigation(
    bound_dc_issue: Any,
    dc_store_copy_repo: Path,
    dc_transport: Any,
    dc_request: Any,
    jira_dc_project: str,
    jira_dc_base_url: str,
    monkeypatch: Any,
) -> None:
    from rebar.llm import gate_source
    from rebar.llm.config import LLMConfig
    from rebar.llm.errors import LLMError
    from rebar.llm.runner import RunRequest, get_runner

    # The cutover the failing rich-text tests run under — the agent's reconcile passes must
    # exercise the SAME rich path, and this env propagates into the reconcile subprocess
    # (engine_env does dict(os.environ)).
    monkeypatch.setenv("REBAR_RECONCILER_RICH_TEXT_CUTOVER", "dc")

    local_id, key = bound_dc_issue
    _register_contract_once()

    namespace = _build_namespace(
        dc_store_copy_repo,
        local_id,
        key,
        jira_dc_project,
        jira_dc_base_url,
        dc_transport,
        dc_request,
    )
    run_python, jira_rest = _make_tools(namespace, dc_request)

    cfg = LLMConfig.from_env(repo_root=str(source_repo_root()))
    runner = get_runner(cfg)
    try:
        runner.preflight()
    except LLMError as exc:
        pytest.skip(f"LLM runtime unavailable: {exc}")

    req = RunRequest(
        system_prompt=_SYSTEM_PROMPT,
        instructions=_instructions(local_id, key),
        config=cfg,
        target={"kind": "jira_dc_richtext_probe", "base_url": jira_dc_base_url, "key": key},
        mode="structured",
        output_schema=CONTRACT_NAME,
        execution_mode="agentic",
        extra_tools=[run_python, jira_rest],
        tool_step_limit=int(os.environ.get("REBAR_LLM_MAX_STEPS", "400")),
    )

    result: dict[str, Any] | None = None
    run_error: str | None = None
    try:
        handle = gate_source.resolve_gate_handle(
            ref=None, source="local", repo_root=str(source_repo_root()), fetch=False
        )
        cfg = gate_source.apply_handle(cfg, handle)
        req.config = cfg
        with gate_source.gate_read_root(handle):
            result = runner.run(req)
    except LLMError as exc:
        run_error = f"{exc}"
    finally:
        out_dir = _write_artifacts(result)

    print("\n" + "=" * 78)
    print(f"AGENTIC RICH-TEXT PROBE — {len(_EVIDENCE)} experiment(s); artifacts in {out_dir}")
    print("=" * 78)
    if result is not None:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(f"agentic run did not return a structured finding: {run_error}")
    print("=" * 78 + "\n")

    # This is an INVESTIGATION, not an assertion of correctness: it must not fail merely
    # because the bug still reproduces. It fails only if the agent could not complete a
    # structured investigation at all (a broken harness/runtime), so a green job means "the
    # finding was produced" and the finding itself is read from the log/artifact.
    assert result is not None, f"agentic investigation produced no finding: {run_error}"
