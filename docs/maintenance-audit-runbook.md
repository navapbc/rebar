# Maintenance audit runbook

This runbook adapts the packaged rebar janitor workflow to this repository. It does not define another audit method. The packaged skill owns the audit phases, evidence schema, verification method, remediation process, approval process, and ticketization process.

## Ownership

Start with [`examples/agent-skills/rebar-janitor/SKILL.md`](../examples/agent-skills/rebar-janitor/SKILL.md). Follow its phase files in order and read each phase file when that phase begins.

This page owns the rebar-specific preparation, durable records, and repository checks that supplement the packaged workflow. Tool versions, machine state, measurements, and findings belong to the audit execution record. They do not belong in this maintained procedure.

## Start and record the audit

Search for an audit ticket before starting. Create one when no existing ticket covers the work. Record the scope, plan, acceptance criteria, file impact, and verification commands on that ticket. Complete plan review and claim the ticket before running audit commands.

Start a session log related to the audit ticket.

```sh
rebar session-log start --summary "Maintenance audit" --relates-to <audit-ticket-alias>
```

The audit ticket preserves the scope, decisions, verification, and final disposition. The session log preserves verbose progress, command inputs, tool versions, observations, and intermediate decisions. Append progress after every phase so another developer or agent can resume the audit.

During phases 1 through 4, record candidate findings and their dispositions in the session log. During phase 5, create tickets only for approved work and link each new ticket to the audit ticket with `discovered_from`. If an approved recommendation establishes an architectural invariant, record that decision in an ADR and use the ticket for its implementation history.

## Run the packaged workflow

Execute the five phases defined by the packaged janitor skill. Use one concern per discovery agent, keep discovery findings free of severity, verify findings independently, compare remediation proposals against community practice and the project record, obtain item-level approval, and ticket only approved work.

The packaged workflow owns its concern list and phase contracts. Do not copy them into this runbook. Read `AGENTS.md`, `CONTRIBUTING.md`, and [`documentation-policy.md`](documentation-policy.md) when the workflow asks for project guidance.

## Rebar project inputs

Run the following commands from the repository root with the worktree virtual environment first on `PATH`. Record the commit, command, parameters, and result in the session log. These commands inspect maintained state without rewriting maintained files.

### Module size

The module limit is an absolute CI gate. The gate reads its positive integer from `.github/module-size-limit.txt` and fails when any `src/rebar/**/*.py` file exceeds that value. `tests/unit/test_module_size_contract.py` reads the same file and provides the repository mirror.

```sh
python -m pytest tests/unit/test_module_size_contract.py -q
```

[ADR 0058](adr/0058-the-module-size-limit-file-is-the-only-loc-ceiling.md) records why `.github/module-size-limit.txt` is the single authoritative upper limit. The target range and anti-fragmentation guidance remain in `AGENTS.md`. Do not convert either guideline into another numeric gate.

### Function complexity

Resolve the project Ruff version, collect the raw C901 census, and run the repository gate.

```sh
ruff --version
ruff check --select C901 --config 'lint.mccabe.max-complexity=15' --exit-zero --statistics src/rebar
python scripts/check_complexity_baseline.py --check
```

The raw census is an audit input. `--exit-zero` allows the command to report baselined findings without making those findings the command verdict. The wrapper is the gate. It rejects new or increased production complexity and permits reductions that make baseline entries stale. Ticket `unafraid-homey-umbrette` records the introduction of this shrink-only ratchet.

### Historical metrics

Use rebar metrics for trends over the interval selected for the audit.

```sh
rebar metrics --since <YYYY-MM-DD> --until <YYYY-MM-DD> --output json
```

Record the selected interval. A metric reported as `unavailable` has no accumulated data for that dimension and must not be interpreted as zero. The packaged discovery phase defines how metrics supplement the one-shot analysis tools.

For the module-size question specifically, `code_health.module_size_trend` and `code_health.cap_change_events` derive their history straight from Git rather than the working tree, so they corroborate the one-shot gate check above across the audited interval: `module_size_trend` samples the tracked module count and the largest module's historical line count (by revision, against that revision's own cap) and `cap_change_events` lists every time the cap in `.github/module-size-limit.txt` itself changed. An empty `cap_change_events.events` list is a real finding — the cap held steady over the interval — not a sign of missing data; only the standard `unavailable` shape (never a zero) means the interval had too little qualifying history to report.

### Documentation and skill checks

Confirm that maintained documentation links resolve and that the packaged skills retain valid frontmatter.

```sh
python scripts/check_docs_index.py --check
python scripts/check_skill_frontmatter.py
```

These checks cover the maintained adapter and packaged skill metadata. They do not replace the verification commands recorded on remediation tickets.

## Historical context

Use tickets and ADRs when an earlier decision or finding explains maintained guidance. Ticket `unafraid-homey-umbrette` provides the history of the function-complexity ratchet. Ticket `scabby-slur-junk` provides an example of an earlier audit finding that was verified, approved, implemented, and closed. ADR 0058 provides the design rationale for the module-size invariant.

Do not copy dated counts, machine state, severity labels, or incident narratives from those records into this runbook. Cite the record and describe only the current mechanism that a future audit must use.

## Close the audit

Before closing the audit ticket, complete these steps:

- Run every verification command recorded on the audit ticket.
- Confirm that every approved recommendation has a ticket and a `discovered_from` link to the audit ticket.
- Record rejected recommendations and their reasons in the audit ticket or session log.
- Append the final outcome and all residual gaps to the session log.
- Check each acceptance criterion only after its evidence exists.
- Transition the audit ticket through the normal completion gate after all required changes reach `origin/main`.

Do not use a force option to bypass plan review, completion verification, or ticket publication.
