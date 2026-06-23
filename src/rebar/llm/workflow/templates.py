"""Scaffolding for ``rebar workflow new`` (WS-B4).

Ships ONE worked, schema-valid 3-step example (scripted fetch -> agent review ->
scripted gate) that doubles as the skeleton ``rebar workflow new`` writes. It opens
with a ``$schema`` modeline so editors with the YAML language server give inline
completion/validation against the version-pinned DSL schema. The literal carries a
``__NAME__`` token rather than a ``str.format`` field so the ``${{ … }}``
expressions in the body are left untouched.
"""

from __future__ import annotations

import re

from rebar.llm.errors import WorkflowParseError

_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")

# A complete, lint-clean 3-step workflow. Keep it in sync with the v1 schema; it is
# parsed + linted by tests/unit/workflow/test_cli.py so it can never drift invalid.
SCAFFOLD_V1 = """\
# yaml-language-server: $schema=https://github.com/navapbc/rebar/schemas/workflow.v1.schema.json
schema_version: "1"
name: __NAME__
description: TODO — describe what this workflow does.

# Workflow inputs, referenced as ${{ inputs.<name> }}.
inputs:
  ticket_id:
    type: string
    required: true

# Steps form a DAG via `needs`; array order is for humans, execution order is the
# topological order. A step is EITHER scripted (`uses:` a built-in) or agentic
# (`prompt:` an .rebar/prompts/<id>.md). Pass values through `with:` and reference
# them by name — never inline ${{ }} into a prompt body.
steps:
  - id: fetch
    uses: fetch_ticket
    with:
      ticket_id: ${{ inputs.ticket_id }}

  - id: review
    prompt: code-quality
    needs: [fetch]
    with:
      context: ${{ steps.fetch.outputs.description }}
    output_schema: review_result
    mode: findings

  - id: gate
    uses: gate
    needs: [review]
    with:
      findings: ${{ steps.review.outputs.findings }}
      policy: default
"""


def scaffold(name: str) -> str:
    """Return a valid skeleton workflow document for ``name``.

    Raises :class:`WorkflowParseError` if ``name`` is not a valid workflow id
    (the same lowercase pattern the schema enforces), so the failure is caught at
    authoring time rather than on the first validate.
    """
    if not _NAME_RE.match(name):
        raise WorkflowParseError(
            f"invalid workflow name {name!r}: use lowercase letters, digits, '-' and "
            f"'_' (must start with a letter)",
            source=name,
        )
    return SCAFFOLD_V1.replace("__NAME__", name)
