#!/usr/bin/env python3
"""Map the pinned Jira Data Center harness's capabilities via an agentic LLM run.

Ticket 259b-b7da-a346-4785: an on-demand, `workflow_dispatch`-only CI job that boots the
digest-pinned Jira DC harness (`tests/external/live_jira_dc/`) and interrogates it with an
Opus agent so we DECLARE the DC environment contract instead of discovering it live, one
question at a time, over ~35-minute CI round trips.

**This is an AUTHORING tool, not a test-path component.** It runs once per image re-pin,
its output is a structured artifact (a candidate environment contract + the raw
request/response evidence behind every answer) that a HUMAN reviews before any of it is
committed as data consumed elsewhere. It never runs on push/PR/schedule (see the workflow),
and the agent is instructed to REPORT findings, never to fix the harness, the repo, or
rebar's own config — the only thing it may mutate is scratch state inside the ephemeral,
throwaway Jira DC container itself (which the calling workflow destroys after the run).

Usage (inside CI, after the harness at ``JIRA_DC_BASE_URL`` answers ``/rest/api/2/serverInfo``):

    python scripts/jira_dc_capability_map.py --output-dir /tmp/jira-dc-map

Requires the ``[agents]`` extra and a model credential the framework can resolve
(``ANTHROPIC_API_KEY`` by default; see ``docs/llm-framework.md``). Mints its own admin
Personal Access Token via ``POST /rest/pat/latest/tokens`` (mirroring
``tests/external/live_jira_dc/conftest.py``'s ``jira_dc_pat`` fixture) — no token is
supplied or stored outside this process.

Lives under ``scripts/`` (not ``src/rebar/``) because it is project-specific CI tooling for
mapping ONE throwaway Jira Data Center image, not a capability the shipped library offers
its consumers: it must never count against the library's module-size budget or the
clean-core optionality gate, and it imports internal reconciler modules
(``rebar_reconciler.adapters.jira*``) that are not part of rebar's public surface. It DOES
import the public ``rebar.llm`` framework as a library, per the ticket's instruction to
reuse rebar's own agentic LLM runtime rather than hand-rolling a client.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent

# The Epic-field readiness vocabulary is SHARED with the live harness fixture and the
# deterministic probe (bugs 9790-cafa-dffa-462e / 941b-f049-5f29-4410). This script imports
# it for its DISCRIMINATOR only (`customfield_count`) — it must NOT call
# `await_required_fields`, which is post-create-only and would deadlock here (see
# `epic_field_report_problem`). It lives next to this file; the insert is defensive so the
# import also works when this script is invoked from elsewhere.
_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)
import jira_dc_field_readiness  # noqa: E402

# Reach the vendored reconciler engine the same way every live_jira_dc conftest does: the
# engine lives at <repo>/src/rebar/_engine and is not importable as `rebar_reconciler` unless
# that directory is put on sys.path first. `tests/_engine_path.py` is the single place that
# layout is encoded, so it — not a re-derived parent-count — is reused here.
_TESTS_DIR = _REPO_ROOT / "tests"
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))
from _engine_path import engine_dir  # noqa: E402

if str(engine_dir()) not in sys.path:
    sys.path.insert(0, str(engine_dir()))

# The four hardcoded vocabularies + the five length-limit constants this job maps against —
# imported LIVE from their single source-of-truth modules (never a frozen copy pasted into
# this script), because the whole point of the mapping run is to diff the CURRENT map against
# the instance. A copy here would silently drift from what the reconciler actually ships.
from rebar_reconciler.adapters.jira.adf import _ADF_DESCRIPTION_LIMIT  # noqa: E402
from rebar_reconciler.adapters.jira.comment_limits import _JIRA_COMMENT_MAX_CHARS  # noqa: E402
from rebar_reconciler.adapters.jira_family.rich_text import WIKI_DESCRIPTION_LIMIT  # noqa: E402
from rebar_reconciler.adapters.jira_family.value_maps import (  # noqa: E402
    JIRA_LABEL_MAX_CHARS,
    JIRA_SUMMARY_MAX_CHARS,
    LOCAL_PRIORITY_TO_JIRA,
    LOCAL_STATUS_TO_JIRA,
    LOCAL_TYPE_TO_JIRA,
    RELATION_TO_JIRA_LINK,
)

from rebar.llm import gate_source  # noqa: E402
from rebar.llm.config import LLMConfig  # noqa: E402
from rebar.llm.contracts import register_contract  # noqa: E402
from rebar.llm.errors import LLMError  # noqa: E402
from rebar.llm.runner import RunRequest, get_runner  # noqa: E402

_HARNESS_DOCKERFILE = _REPO_ROOT / "tests" / "external" / "live_jira_dc" / "Dockerfile"
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")


def harness_image_digest(dockerfile: Path | None = None) -> str | None:
    """Read the harness image digest from the ONE place that decides which image is
    built: the vendored Dockerfile's ``FROM`` line — never a second, hand-copied literal
    that could drift from the first. ``dockerfile`` defaults to the real vendored harness
    Dockerfile, resolved relative to the repo root; pass an override to make this
    derivation testable against a fixture.

    Deliberately does not raise: this runs mid-way through a long, billable live CI run,
    and this value is metadata about the run, not something the run depends on — an
    aborted run over a missing/malformed pin would throw away the evidence the run exists
    to collect. A missing Dockerfile or a ``FROM`` line without an ``@sha256:`` digest
    both report as "no digest" (``None``) rather than falling back to a remembered value,
    which would silently mislabel the artifact just as badly as the bug this replaces.
    """
    path = dockerfile if dockerfile is not None else _HARNESS_DOCKERFILE
    try:
        text = path.read_text()
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("FROM"):
            found = _DIGEST_RE.search(line)
            if found:
                return found.group(0)
    return None


_DEFAULT_BASE_URL = "http://localhost:2990/jira"
_PAT_NAME_PREFIX = "rebar-259b-map-"
CONTRACT_NAME = "jira_dc_capability_map"

# One capture of every raw request/response the agent's tool makes — the evidence artifact.
# Module-level (not a class) because the tool closures created in `make_jira_tool` all need to
# append to the SAME list regardless of how many times pydantic-ai calls them.
_EVIDENCE: list[dict[str, Any]] = []


# ─────────────────────────────────────────────────────────────────────────────────────
# Raw REST plumbing (stdlib only — no rebar transport in the path; several checklist
# items are explicitly "raw REST, no rebar in the path", and using the SAME helper for
# every call keeps that true uniformly rather than per-question).
# ─────────────────────────────────────────────────────────────────────────────────────


def _raw_request(
    base_url: str,
    method: str,
    path: str,
    *,
    token: str | None = None,
    basic_auth: tuple[str, str] | None = None,
    body: dict | None = None,
    timeout: float = 30,
) -> tuple[int, Any]:
    url = f"{base_url.rstrip('/')}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    elif basic_auth:
        user, password = basic_auth
        creds = base64.b64encode(f"{user}:{password}".encode()).decode()
        req.add_header("Authorization", f"Basic {creds}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        status = exc.code
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return 0, f"<transport error: {exc!r}>"
    if not raw.strip():
        return status, None
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, raw


def _mint_admin_pat(base_url: str, admin_user: str, admin_password: str) -> str:
    """Mint a throwaway PAT via ``POST /rest/pat/latest/tokens`` (DC 8.14+), mirroring
    ``tests/external/live_jira_dc/conftest.py``'s ``jira_dc_pat`` fixture. A short
    ``expirationDuration`` (in days) matches that fixture's convention; this harness is
    torn down at the end of the workflow run regardless."""
    import random
    import string

    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
    name = f"{_PAT_NAME_PREFIX}{suffix}"
    status, created = _raw_request(
        base_url,
        "POST",
        "/rest/pat/latest/tokens",
        basic_auth=(admin_user, admin_password),
        body={"name": name, "expirationDuration": 1},
    )
    if status not in (200, 201) or not isinstance(created, dict) or not created.get("rawToken"):
        raise RuntimeError(f"PAT creation failed: {status} {created!r}")
    return str(created["rawToken"])


def make_jira_tool(base_url: str, token: str):
    """Build the agent's ONLY mutation-capable tool: a raw REST call against the pinned
    Jira DC harness, Bearer-authenticated with the freshly minted PAT. Every call is
    recorded into the module-level ``_EVIDENCE`` list with a stable id BEFORE the tool
    returns, so the final artifact can carry the full raw request/response for every
    answer — the ticket's hard requirement that answers be traceable to evidence, not to
    a model's summary."""

    def jira_request(method: str, path: str, body: dict | None = None) -> str:
        """Make ONE raw REST call against the pinned Jira Data Center harness.

        ``method`` is GET/POST/PUT/DELETE. ``path`` is a REST path such as
        "/rest/api/2/field" (never a full URL — this tool always targets the pinned
        harness). ``body`` is an optional JSON-serializable payload for POST/PUT.

        This is a THROWAWAY, ephemeral instance stood up solely for this run and
        destroyed afterward — you may freely create/delete scratch projects, issues,
        issue types, fields, and screens on it as your checklist requires. Never call
        any host other than this harness (you have no tool that could reach one), and
        never treat a mutation here as something to "fix" — record what happened as
        evidence for your structured answer.

        Returns a short string: an evidence id, the HTTP status, and the (possibly
        truncated for context budget) response body. The FULL raw request and response
        are captured separately for the final artifact — cite the evidence id in your
        structured answer rather than re-quoting the body verbatim.
        """
        status, parsed = _raw_request(base_url, method.upper(), path, token=token, body=body)
        evidence_id = f"req-{len(_EVIDENCE):04d}"
        _EVIDENCE.append(
            {
                "id": evidence_id,
                "method": method.upper(),
                "path": path,
                "request_body": body,
                "status": status,
                "response_body": parsed,
                "ts": time.time(),
            }
        )
        rendered = json.dumps(parsed, default=str)
        if len(rendered) > 4000:
            rendered = rendered[:4000] + " …(truncated; see evidence artifact for the full body)"
        return f"[{evidence_id}] {method.upper()} {path} -> {status}\n{rendered}"

    return jira_request


# ─────────────────────────────────────────────────────────────────────────────────────
# The structured-output contract (rebar.llm.contracts seam, §4a of docs/reuse-surface.md).
# ─────────────────────────────────────────────────────────────────────────────────────


def _build_contract() -> type:
    from pydantic import BaseModel, Field

    class VocabVerdict(BaseModel):
        """One rebar vocabulary entry diffed against the live instance."""

        local_key: str
        local_value: str
        instance_value: str | None = None
        verdict: str  # "present" | "absent" | "present_but_different"
        detail: str = ""
        evidence_ids: list[str] = Field(default_factory=list)

    class LinkTypeInfo(BaseModel):
        name: str
        inward: str
        outward: str

    class DirectionCheck(BaseModel):
        """The end-to-end link-direction experiment for one rebar relation."""

        relation: str
        expected_jira_link_name: str
        expected_swap_endpoints: bool
        a_key: str | None = None
        b_key: str | None = None
        a_side_observed_name: str | None = None
        b_side_observed_name: str | None = None
        matches_expectation: bool | None = None
        detail: str = ""
        evidence_ids: list[str] = Field(default_factory=list)

    class LengthLimitCheck(BaseModel):
        field: str  # summary | description | comment | label
        hardcoded_limit: int
        at_limit_minus_1: str
        at_limit: str
        at_limit_plus_1: str
        readback_matched_write: bool | None = None
        detail: str = ""
        evidence_ids: list[str] = Field(default_factory=list)

    class ProjectTemplateResult(BaseModel):
        template_key: str
        template_name: str
        issue_types_yielded: list[str] = Field(default_factory=list)
        yields_epic: bool = False
        evidence_ids: list[str] = Field(default_factory=list)

    class WorkflowTransition(BaseModel):
        name: str
        to_status: str

    class WorkflowStatus(BaseModel):
        name: str
        category: str
        transitions_from: list[WorkflowTransition] = Field(default_factory=list)
        reachable_from_initial_state: bool | None = None

    class IssueTypeWorkflow(BaseModel):
        issue_type: str
        is_subtask: bool = False
        workflow_name: str | None = None
        statuses: list[WorkflowStatus] = Field(default_factory=list)

    class Experiment(BaseModel):
        name: str
        outcome: str
        detail: str
        evidence_ids: list[str] = Field(default_factory=list)

    class JiraDcCapabilityMap(BaseModel):
        image_digest: str
        jira_version: str
        licensed_applications: list[str] = Field(default_factory=list)

        project_templates: list[ProjectTemplateResult] = Field(default_factory=list)
        epic_link_field_id: str | None = None
        epic_name_field_id: str | None = None
        epic_link_on_standard_edit_screen: bool | None = None
        epic_name_on_epic_create_screen: bool | None = None

        issue_type_vocab_diff: list[VocabVerdict] = Field(default_factory=list)
        priority_vocab_diff: list[VocabVerdict] = Field(default_factory=list)
        status_vocab_diff: list[VocabVerdict] = Field(default_factory=list)
        workflows: list[IssueTypeWorkflow] = Field(default_factory=list)

        link_type_inventory: list[LinkTypeInfo] = Field(default_factory=list)
        link_relation_vocab_diff: list[VocabVerdict] = Field(default_factory=list)
        link_direction_checks: list[DirectionCheck] = Field(default_factory=list)
        unmapped_relations_expressible: list[VocabVerdict] = Field(default_factory=list)

        length_limits: list[LengthLimitCheck] = Field(default_factory=list)
        jira_text_field_character_limit_property: str | None = None

        experiments: list[Experiment] = Field(default_factory=list)
        falsifier_37e7_parent_editmeta: str = ""
        falsifier_37e7_update_vs_fields_null: str = ""
        falsifier_1a9f_ac1_subtask_put_parent: str = ""
        epic_link_write_takes_effect: str = ""

        findings: list[str] = Field(default_factory=list)
        summary: str = ""

    return JiraDcCapabilityMap


register_contract(CONTRACT_NAME, _build_contract)


# ─────────────────────────────────────────────────────────────────────────────────────
# Prompting
# ─────────────────────────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """You are the Jira Data Center environment-mapping agent for the rebar
project (ticket 259b-b7da-a346-4785). You are running against a REAL, THROWAWAY Jira Data
Center 8.17.1 instance stood up solely for this run — it will be destroyed the moment this
job ends.

**You REPORT, you do not FIX.** You are not here to make rebar's assumptions correct; you
are here to find out whether they already ARE correct, and to record exactly what you
observed with the raw evidence behind it. If something disagrees with rebar's hardcoded
vocabulary, that is a FINDING to record — never something for you to patch, and never
something to characterize as "should be changed" in the instance either. You have exactly
one tool that can mutate anything, `jira_request`, and it is scoped to this one throwaway
instance — you have no other tool that could reach a file, a repository, or any other host.
Do not use any ticket-mutating tool even if one appears to be available to you; this run
must not write to rebar's own ticket store.

Batch your REST calls efficiently — this instance took minutes to boot and you have a
generous but FINITE tool-call budget for the whole checklist below, so do not repeat a call
whose answer you already have, and prefer read endpoints that answer several checklist items
in one shot (e.g. GET a full workflow's statuses+transitions once rather than one call per
status).

Every field in your final structured answer that reports an observation MUST be traceable
to one or more `jira_request` evidence ids (the `[req-NNNN]` tag each tool response carries)
recorded in that field's `evidence_ids` — an answer with no evidence id is indistinguishable
from a guess and defeats the entire point of this run.
"""

_INSTRUCTIONS_TEMPLATE = """## Checklist (answer every item; an empty/omitted answer is a
finding, not a silent skip)

### 1. Inventory
- The exact Jira version + which licensed applications are present (does Jira Software /
  the `Epic` type exist at all?) — `GET /rest/api/2/serverInfo`,
  `GET /rest/api/2/application-properties` or an equivalent.
- Every project template the instance offers —
  `GET /rest/project-templates/latest/templates` — and, for a project created from EACH
  candidate template (`POST /rest/api/2/project`), the issue types it actually yields
  (`GET /rest/api/2/issuetype/project?projectId=...` or equivalent) and whether `Epic` is
  among them.
- The full field inventory — `GET /rest/api/2/field` — specifically the ids of `Epic Link`
  and `Epic Name`.
- Whether `Epic Link` is on a standard issue's EDIT screen and `Epic Name` is on the Epic
  CREATE screen by default (read the relevant screen scheme / `editmeta`).
- Every issue type the default project yields, each with its `subtask` flag, and whether
  each name in rebar's LOCAL_TYPE_TO_JIRA map exists: {local_type_to_jira}

### 2. Workflows and statuses (per issue type — they can differ)
- Which workflow is bound to each issue type.
- Every status in that workflow, its name AND its status CATEGORY (distinct concepts).
- For each status, the transitions available FROM it: the transition NAME and its
  DESTINATION status name side by side (this is the exact pair bug 7f93 conflated — a
  transition's name is NOT its destination status's name).
- Whether every value in rebar's LOCAL_STATUS_TO_JIRA map is reachable, and by which
  transition from which starting state (a status that EXISTS but is UNREACHABLE from the
  initial state is a different defect from a misspelled one): {local_status_to_jira}

### 3. Priorities and link types
- `GET /rest/api/2/priority` — does every value in rebar's LOCAL_PRIORITY_TO_JIRA map exist?
  {local_priority_to_jira}
- `GET /rest/api/2/issueLinkType` in FULL — for every type, its `name`, `inward`, and
  `outward` strings (not just whether "Blocks"/"Relates" exist).
- Whether every rebar relation's target Jira link type name exists:
  {relation_to_jira_link}
  Note `blocks` and `depends_on` map to the SAME Jira link type name and are distinguished
  ONLY by the boolean (whether the endpoints are swapped) — a direction error here does
  NOT produce a missing link or an error, it produces a link that LOOKS right and points
  the wrong way. So also run an END-TO-END experiment: create two throwaway issues A and B,
  write a `blocks`-shaped link from A to B (matching how rebar's outbound mapper would
  construct it: type name + endpoint order per the map above), then read back the RAW issue
  JSON for BOTH A and B and record which side reports which of the inward/outward names.
  Repeat for `depends_on` and confirm it lands as the INVERSE of `blocks`, not a duplicate.
  Do the same read-back for `relates_to` (symmetric — a direction bug here is undetectable
  from the link alone).
- Record which of rebar's three UNMAPPED relations (`duplicates`, `supersedes`,
  `discovered_from`, `caused_by`) the instance COULD express out of the box (e.g. stock
  Jira's `Duplicate` / `Cloners` types) — this is a finding to file, not to fix.

### 4. Length limits (five hardcoded constants; measure each live)
- summary: hardcoded max {jira_summary_max_chars} (inclusive)
- label: hardcoded max {jira_label_max_chars} (inclusive)
- description (DC wiki text): hardcoded max {wiki_description_limit}
- comment body: hardcoded max {jira_comment_max_chars}
- Cloud ADF description margin (informational only — NOT testable on this DC harness):
  {adf_description_limit}
For summary, description, and comment (label is created via a different endpoint than
description/comment; test what "label" actually validates against on this instance), write
at limit-1, limit, and limit+1 chars and record for EACH: accepted intact, rejected (with
the exact error shape — note that some Jira write paths are known to report success even on
an over-length rejection elsewhere in rebar's stack, so look closely at the actual HTTP
status/body), or accepted-but-SILENTLY-TRUNCATED (detect this ONLY by reading the value back
and comparing lengths — never infer it from a successful write status alone; this is the
most dangerous and least visible outcome).
Also read `jira.text.field.character.limit` from
`GET /rest/api/2/application-properties` (or the equivalent instance-properties endpoint)
and record its raw value (`0` means unlimited per bug 049e) rather than assuming 32767.

### 5. Configuration-hook experiments
- Can the admin PAT create a NEW issue type via `POST /rest/api/2/issuetype` and add it to
  a project's issue-type scheme — the deterministic alternative to relying on a particular
  project template?
- Can it add a field to a screen, confirmed by reading `editmeta` back afterward?
- **Falsifier [rebar:37e7-d751-0042-4b94]**: what operations does `parent` expose in a
  SUB-TASK's `editmeta`, and does `{{"update":{{"parent":[{{"set":null}}]}}}}` behave
  differently from `{{"fields":{{"parent":null}}}}` when PUT to a sub-task? Record BOTH raw
  responses in `falsifier_37e7_parent_editmeta` / `falsifier_37e7_update_vs_fields_null` —
  a difference here overturns that ticket's NOT-RESOLVABLE verdict.
- **Falsifier [rebar:1a9f-50c0-e7a5-4fda] AC1**: does a sub-task `PUT` with
  `fields.parent` set return a 2xx-and-ignore, or a 4xx? Raw REST only — do not route this
  through any rebar transport code. Record the raw response in
  `falsifier_1a9f_ac1_subtask_put_parent`.
- Once an Epic exists (from step 1): does writing `Epic Link` on a child issue actually
  take effect (read the child back and confirm)? Record in
  `epic_link_write_takes_effect` — this exact question has failed to reach rebar three
  times (change 1311).

## Base URL
Every `jira_request` path is relative to this harness's Jira base
(`{base_url}` — note the `/jira` context path is already applied by the tool, so pass paths
like "/rest/api/2/field", never the full URL).

## Output
Emit ONE final structured `jira_dc_capability_map` answer covering every field above. Every
vocabulary-diff entry needs an explicit `verdict` of "present", "absent", or
"present_but_different" (never leave it implicit). Use `findings` for anything you observed
that looks like a product bug, a harness-config gap, or a genuine mismatch worth a ticket —
deciding what to DO about a finding is explicitly not your job.
"""


def _instructions(base_url: str) -> str:
    return _INSTRUCTIONS_TEMPLATE.format(
        base_url=base_url,
        local_type_to_jira=json.dumps(LOCAL_TYPE_TO_JIRA),
        local_status_to_jira=json.dumps(LOCAL_STATUS_TO_JIRA),
        local_priority_to_jira=json.dumps(LOCAL_PRIORITY_TO_JIRA),
        relation_to_jira_link=json.dumps({k: list(v) for k, v in RELATION_TO_JIRA_LINK.items()}),
        jira_summary_max_chars=JIRA_SUMMARY_MAX_CHARS,
        jira_label_max_chars=JIRA_LABEL_MAX_CHARS,
        wiki_description_limit=WIKI_DESCRIPTION_LIMIT,
        jira_comment_max_chars=_JIRA_COMMENT_MAX_CHARS,
        adf_description_limit=_ADF_DESCRIPTION_LIMIT,
    )


# ─────────────────────────────────────────────────────────────────────────────────────
# Post-run validation of the Epic-field half of the report (bug 4a6d-5bbc-44f4-4a56)
# ─────────────────────────────────────────────────────────────────────────────────────

#: The contract fields whose absence this validation refuses to take on trust.
_EPIC_FIELD_KEYS = ("epic_link_field_id", "epic_name_field_id")


def _last_field_inventory(evidence: list[dict[str, Any]]) -> tuple[int, object] | None:
    """The most recent USABLE ``GET /rest/api/2/field`` in the evidence log.

    "Usable" is exactly ``jira_dc_field_readiness``'s definition — a 200 whose body is a
    list of field dicts. A 401/503 or an error string is skipped rather than counted as an
    empty inventory, so a transport failure never masquerades as "this instance has no
    custom fields". Returns ``None`` when the run captured no usable field read at all.
    """
    for entry in reversed(evidence):
        if str(entry.get("method", "")).upper() != "GET":
            continue
        path = str(entry.get("path", "")).split("?", 1)[0].rstrip("/")
        if path != jira_dc_field_readiness.FIELD_PATH:
            continue
        status = entry.get("status")
        body = entry.get("response_body")
        status_int = status if isinstance(status, int) else 0
        if jira_dc_field_readiness.customfield_count(status_int, body) is None:
            continue
        return status_int, body
    return None


def epic_field_report_problem(result: dict[str, Any], evidence: list[dict[str, Any]]) -> str | None:
    """Why the run's Epic-field answer cannot be vouched for — or ``None`` if it can.

    THE DEFECT THIS CLOSES (bug 4a6d-5bbc-44f4-4a56). This script's report is the authority
    a human uses to update ``_REQUIRED_FIELDS`` / ``_PROJECT_TEMPLATE`` in
    ``tests/external/live_jira_dc/conftest.py``. ``Epic Link``/``Epic Name`` are GreenHopper
    custom fields provisioned on the FIRST Jira Software project create, not at plugin start
    (run 30981084637 — bug 941b-f049-5f29-4410). An agent that read ``/rest/api/2/field``
    before creating its first project therefore sees a perfectly healthy instance with 27
    system fields and zero ``customfield_*`` entries, and can truthfully report "no Epic Link
    id" — which a reader will read as "this image dropped the Epic fields". The report does
    not fail, so nothing makes them notice.

    THE DISCRIMINATOR IS THE INVENTORY, NOT THE CLOCK, so this is a post-run check on
    recorded evidence and NOT a pre-run wait. A pre-run ``await_required_fields`` would wait
    for a thing only the agent's own project creates can produce — the deadlock 941b landed
    to remove — and that module bans the call site in terms.

    * Epic ids reported **and** non-empty → nothing to check.
    * An id reported absent while the inventory holds **other** ``customfield_*`` entries →
      provisioning demonstrably happened, so the absence is a real degrade. Accepted.
    * An id reported absent while the inventory holds **zero** ``customfield_*`` entries, or
      while no usable field read was captured at all → the claim is indistinguishable from
      "no Jira Software project existed yet". UNVERIFIED; the caller fails the job.

    A null optional dumps OUT of the structured payload entirely (``model_dump
    (exclude_none=True)`` in ``rebar.llm.findings.finalize_outcome``), so "reported absent"
    means a missing key just as much as an explicit ``None`` or ``""`` — all three are read
    the same way here.
    """
    absent = [key for key in _EPIC_FIELD_KEYS if not result.get(key)]
    if not absent:
        return None

    observation = _last_field_inventory(evidence)
    if observation is None:
        inventory = (
            f"no usable GET {jira_dc_field_readiness.FIELD_PATH} was captured in this run's "
            f"evidence ({len(evidence)} REST call(s) recorded)"
        )
    else:
        status, body = observation
        count = jira_dc_field_readiness.customfield_count(status, body)
        if count:
            return None
        inventory = jira_dc_field_readiness.describe_inventory(status, body)

    return (
        f"UNVERIFIED: the run reports {', '.join(absent)} absent, but the field inventory it "
        f"recorded holds NO customfield_* entries — so this answer cannot distinguish 'this "
        f"image no longer ships the Epic fields' from 'no Jira Software project had been "
        f"created yet when the inventory was read'. GreenHopper provisions Epic Link/Epic "
        f"Name on the FIRST Jira Software project create, measured at "
        f"{jira_dc_field_readiness.PROVISIONING_TO_FIELDS_VISIBLE_S:.4f}s after the 201 on "
        f"run 30981084637 (bug 941b-f049-5f29-4410); before that create a healthy instance "
        f"shows 27 system fields and zero customfield_* entries. Do NOT record this answer as "
        f"the environment contract. Re-run the map and have the agent re-read the field "
        f"inventory AFTER its first successful project create. Last observation: {inventory}"
    )


# ─────────────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.environ.get("JIRA_DC_BASE_URL", _DEFAULT_BASE_URL))
    parser.add_argument("--admin-user", default=os.environ.get("JIRA_DC_ADMIN", "admin"))
    parser.add_argument(
        "--admin-password", default=os.environ.get("JIRA_DC_ADMIN_PASSWORD", "admin")
    )
    parser.add_argument(
        "--output-dir", default=os.environ.get("JIRA_DC_MAP_OUTPUT_DIR", "jira-dc-capability-map")
    )
    parser.add_argument(
        "--dockerfile",
        type=Path,
        default=None,
        help=(
            "Override the Dockerfile the harness image digest is read from "
            "(default: the vendored tests/external/live_jira_dc/Dockerfile)."
        ),
    )
    parser.add_argument(
        "--print-digest",
        action="store_true",
        help=(
            "Print the derived harness image digest and exit immediately — no Jira "
            "contact, no PAT, no LLM, no files written. Lets a human confirm which "
            "image a run will map without a ~35-minute live run."
        ),
    )
    args = parser.parse_args(argv)

    if args.print_digest:
        digest = harness_image_digest(args.dockerfile)
        print(digest if digest is not None else "unpinned")
        return 0

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    cfg = LLMConfig.from_env(repo_root=str(_REPO_ROOT))
    runner = get_runner(cfg)
    try:
        runner.preflight()
    except LLMError as exc:
        print(f"::error::LLM runtime unavailable: {exc}", file=sys.stderr)
        return 1

    try:
        token = _mint_admin_pat(args.base_url, args.admin_user, args.admin_password)
    except (RuntimeError, urllib.error.URLError, OSError) as exc:
        print(
            f"::error::could not mint an admin PAT against {args.base_url}: {exc}",
            file=sys.stderr,
        )
        return 1

    jira_tool = make_jira_tool(args.base_url, token)

    req = RunRequest(
        system_prompt=_SYSTEM_PROMPT,
        instructions=_instructions(args.base_url),
        config=cfg,
        target={"kind": "jira_dc_harness", "base_url": args.base_url},
        mode="structured",
        output_schema=CONTRACT_NAME,
        execution_mode="agentic",
        extra_tools=[jira_tool],
    )

    exit_code = 0
    result: dict[str, Any] | None = None
    try:
        # A standalone, one-off "local mode" gate session (rebar.llm.gate_source), the
        # sanctioned seam for a NEW agentic operation outside the review/verify gates
        # (see rebar.llm.gate_context.assert_gated). This op does not read repo files or ticket
        # state for its OWN purpose (see the system prompt), so `local` (the in-place
        # checkout, no snapshot materialization) is the right mode — never `attested`,
        # which would fetch/materialize a snapshot this op has no use for.
        handle = gate_source.resolve_gate_handle(
            ref=None, source="local", repo_root=str(_REPO_ROOT), fetch=False
        )
        cfg = gate_source.apply_handle(cfg, handle)
        req.config = cfg
        with gate_source.gate_read_root(handle):
            result = runner.run(req)
    except LLMError as exc:
        print(f"::error::the mapping run failed: {exc}", file=sys.stderr)
        exit_code = 1
    finally:
        # Bug 4a6d-5bbc-44f4-4a56: an answer that declares the Epic fields absent is only
        # believable if the recorded inventory shows provisioning already happened. Checked
        # HERE, on the evidence the run captured, because the fields' precondition is the
        # agent's own first project create — nothing before the run could have produced them.
        epic_problem = None if result is None else epic_field_report_problem(result, _EVIDENCE)
        if epic_problem is not None:
            print(f"::error::{epic_problem}", file=sys.stderr)
            exit_code = 1
        (out_dir / "evidence.json").write_text(json.dumps(_EVIDENCE, indent=2, default=str))
        if result is not None:
            (out_dir / "capability_map.json").write_text(json.dumps(result, indent=2, default=str))
        (out_dir / "run_metadata.json").write_text(
            json.dumps(
                {
                    "harness_image_digest": harness_image_digest(args.dockerfile),
                    "base_url": args.base_url,
                    "model": cfg.model,
                    "rest_call_count": len(_EVIDENCE),
                    "run_succeeded": result is not None,
                    # "verified" | "unverified" | "not_run" — recorded in the artifact
                    # itself, so a human reading capability_map.json out of the CI zip
                    # (where the ::error:: annotation is not attached) still learns that
                    # the Epic-field answer was refused.
                    "epic_field_report": (
                        "not_run"
                        if result is None
                        else ("unverified" if epic_problem is not None else "verified")
                    ),
                    "epic_field_report_detail": epic_problem,
                },
                indent=2,
            )
        )

    print(
        f"wrote {out_dir}/{{capability_map.json,evidence.json,run_metadata.json}} "
        f"({len(_EVIDENCE)} REST calls captured)"
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
