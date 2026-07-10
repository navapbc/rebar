# Example Claude Code skills

This directory holds a small set of [Claude Code](https://docs.claude.com/en/docs/claude-code)
**skills** as worked examples of how an agentic development workflow can be built around rebar.

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
skill's directory into your Claude Code skills location and invoke it by its name — see the
[Claude Code skills documentation](https://docs.claude.com/en/docs/claude-code) for how skills are
discovered and run. The rebar-specific steps inside a skill (for example, how changes are reviewed
and landed) are written to defer to a project's own documentation, so they can be retargeted to
another project's workflow.
