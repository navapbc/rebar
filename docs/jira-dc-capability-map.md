# Jira DC capability map (ticket 259b-b7da-a346-4785)

An on-demand, `workflow_dispatch`-only CI job
(`.github/workflows/jira-dc-capability-map.yml`) that boots the pinned Jira Data Center
harness (`tests/external/live_jira_dc/`) and runs an agentic Opus mapping pass
(`scripts/jira_dc_capability_map.py`, built on `rebar.llm`'s own agent runtime — see
`docs/llm-framework.md` / `docs/reuse-surface.md` §2) against it, answering a fixed
checklist instead of an open-ended "go explore":

- issue-type inventory, project templates, and which one yields `Epic` (and whether the
  instance is even licensed for it);
- the `Epic Link` / `Epic Name` field ids, and whether they sit on the default edit/create
  screens;
- every workflow's statuses (name **and** category) and the transitions available from
  each, paired with their destination status — the exact "transition name is not the
  destination status name" distinction that caused bug 7f93;
- every value in rebar's four hardcoded Jira-family vocabularies
  (`LOCAL_PRIORITY_TO_JIRA`, `LOCAL_TYPE_TO_JIRA`, `LOCAL_STATUS_TO_JIRA`,
  `RELATION_TO_JIRA_LINK`), diffed against the live instance as present / absent /
  present-but-different, including an end-to-end link-**direction** experiment (`blocks` vs
  `depends_on` share one Jira link type name and differ only by endpoint order);
- the five hardcoded Jira length limits (summary, label, DC description, comment,
  the Cloud ADF margin), each measured live at limit-1/limit/limit+1 with a **read-back**
  comparison (the only way to catch silent accept-and-truncate);
- the `37e7`/`1a9f` link-direction/parent-clearing falsifiers, executed as raw REST calls.

## What it produces

An artifact (`jira-dc-capability-map`) with three files: `capability_map.json` (the
agent's structured answer to every checklist item, each field carrying the
`evidence_ids` of the raw REST calls that back it), `evidence.json` (every raw
request/response the agent made, in call order), and `run_metadata.json` (the image
digest, base URL, model, and call count).

**The artifact is authored, not live.** A human reviews it and is responsible for landing
whatever should become committed data (e.g. an updated `docs/jira-dc-value-map.md` or a
harness setup assertion that reads it) — the workflow itself never commits anything, and
the agent is explicitly instructed to *report* mismatches as findings, never to fix the
harness, the repository, or rebar's own configuration. Any finding that implicates one of
rebar's own hardcoded vocabularies or limits should be filed as a ticket citing the
relevant `evidence_ids`, rather than acted on directly from the artifact.

## When to re-run it

Re-run whenever the harness's pinned base image changes — i.e. whenever
`tests/external/live_jira_dc/Dockerfile`'s `FROM …@sha256:…` digest is bumped. The
mapping job builds that same Dockerfile, so it always maps the image the harness will
actually run against; a stale map after a re-pin describes an image nothing runs anymore.
It is also reasonable to re-run after any change to rebar's own Jira-family vocabularies
(`src/rebar/_engine/rebar_reconciler/adapters/jira_family/value_maps.py` and neighbors) to
re-confirm the diff still reads clean.

Dispatch from the Actions tab (needs the `ANTHROPIC_API_KEY` repository secret); never on
push, PR, or schedule — `tests/unit/test_jira_dc_capability_map_workflow.py` asserts the
trigger is `workflow_dispatch`-only.
