# Example agent skills

This directory holds a small set of **agent skills** as worked examples of how an agentic
development workflow can be built around rebar. Each is written in the `SKILL.md` format — a
plain-Markdown skill file (a `name`/`description` front-matter block plus a protocol body).
The format is portable: it started with Claude Code's Agent Skills and is now read by a
growing set of coding-agent harnesses (for example Codex, Cursor, Copilot, and Gemini CLI),
which each discover skills from their own tool-specific location. Because the format is
shared, these examples are useful to any harness, not just one.

They are illustrative, not required: rebar does not depend on them, and you do not need them to
use rebar. They exist to show one way an agent can plan, build, and track work with rebar as the
system of record — from shaping a ticket, to implementing it under test discipline, to keeping the
tracker honest over time.

## The skills

| Skill | What it demonstrates |
|-------|----------------------|
| `rebar-brainstorm` | A one-question-at-a-time design loop that turns a vague idea into a spec ready to implement, grounded in research and self-critique. |
| `rebar-implement` | Executing a decomposed epic end-to-end under strict test-first discipline — claiming each ticket, implementing leaves, and landing the work through the project's review flow — using rebar to drive and record every step. |
| `rebar-debug` | A hypothesis-driven, root-cause-first debugging protocol with a strict understand-then-repair separation. |
| `rebar-janitor` | A codebase-health pipeline that finds, verifies, and files maintainability work as tracked tickets, without editing code itself. |

Each skill lives in its own directory with a `SKILL.md` (and, where useful, supporting reference
files). Open any `SKILL.md` to read the full protocol.

## Using them

These are examples to read, adapt, and borrow from. To try one in your own environment, copy the
skill's directory into your harness's skills location and invoke it by its name. Where a harness
looks for skills — and how it names the invocation — differs per tool, so consult your harness's
own skills documentation for the exact path and trigger. The rebar-specific steps inside a skill
(for example, how changes are reviewed and landed) are written to defer to a project's own
documentation, so they can be retargeted to another project's workflow.

## The shared test-design standard (`shared/test-design.md`)

`shared/test-design.md` is the canonical copy of the test-design standard the skills
apply when authoring tests or testing acceptance criteria. Each consuming skill —
`rebar-debug`, `rebar-implement`, `rebar-brainstorm` — ships its own `test-design.md`
as a **byte-identical real-file copy**, so every skill directory is self-contained in
any load context (a checkout, a symlinked skills dir, a plain copy). `rebar-janitor`
takes no copy: it authors no tests.

Edit the **canonical** file, then sync the copies:

```sh
for s in rebar-debug rebar-implement rebar-brainstorm; do
  cp examples/agent-skills/shared/test-design.md examples/agent-skills/$s/test-design.md
done
```

A gating unit test (`tests/unit/test_skill_shared_sync.py`, run by `make test`) fails
when any copy diverges from the canonical file, so drift cannot land.
