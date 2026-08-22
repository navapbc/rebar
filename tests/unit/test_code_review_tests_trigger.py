"""The `tests` overlay fires from DIFF CONTENT when a change adds a declaration and touches no
test file (story 2e0f-e4dc-ee9d-4855).

Every assertion here is on OBSERVABLE selection behaviour — the overlay id set returned by
``registry.content_triggered_overlays`` and the ``include_tests`` flag emitted by the
``overlay_union`` workflow step — never on private helper names or on source text.
"""

from __future__ import annotations

import pytest

from rebar.llm.code_review import assemble, registry
from rebar.llm.prompting import prompts


def _diff(path: str, added: list[str], removed: list[str] | None = None) -> str:
    """A minimal but real `git diff` fragment for one file."""
    removed = removed or []
    body = "".join(f"-{line}\n" for line in removed) + "".join(f"+{line}\n" for line in added)
    return (
        f"diff --git a/{path} b/{path}\n"
        f"--- a/{path}\n"
        f"+++ b/{path}\n"
        f"@@ -1,{max(len(removed), 1)} +1,{max(len(added), 1)} @@\n"
        f"{body}"
    )


def _selects_tests(diff_text: str, changed: list[str]) -> bool:
    return "tests" in registry.content_triggered_overlays(diff_text, changed_files=changed)


# ── the core rule ────────────────────────────────────────────────────────────────────────────
def test_added_definition_with_no_test_file_selects_tests() -> None:
    diff = _diff("src/rebar/thing.py", ["def compute(x):", "    return x + 1"])
    assert _selects_tests(diff, ["src/rebar/thing.py"])


def test_composed_context_selects_tests_like_the_raw_diff() -> None:
    """The step scans `assemble_diff.outputs.context`, not a raw diff — both must select."""
    changed = ["src/rebar/thing.py"]
    diff = _diff("src/rebar/thing.py", ["def compute(x):", "    return x + 1"])
    context = assemble.compose_diff_context(changed, diff)
    assert _selects_tests(context, changed)


def test_overlay_union_sets_include_tests_from_content() -> None:
    """End-to-end through the workflow step that consumes the trigger."""
    from rebar.llm.code_review import workflow_ops

    changed = ["src/rebar/thing.py"]
    diff = _diff("src/rebar/thing.py", ["def compute(x):", "    return x + 1"])

    class _Ctx:
        def __init__(self) -> None:
            self.inputs = {
                "changed_files": changed,
                "diff_text": assemble.compose_diff_context(changed, diff),
            }
            self.repo_root = None

    out = workflow_ops.overlay_union(_Ctx())
    assert out["include_tests"] is True
    assert "tests" in out["content_overlays"]


def test_no_declaration_added_does_not_select_tests() -> None:
    diff = _diff("src/rebar/thing.py", ["    # a clarifying comment", ""])
    assert not _selects_tests(diff, ["src/rebar/thing.py"])


def test_redeclaring_the_same_name_in_the_same_file_does_not_select() -> None:
    """An annotation-only / re-wrapped signature rewrite adds no NEW declaration."""
    diff = _diff(
        "src/rebar/thing.py",
        ["def compute(", "    x: int | None = None,", ") -> int:"],
        ["def compute(x=None) -> int:"],
    )
    assert not _selects_tests(diff, ["src/rebar/thing.py"])


def test_a_move_between_files_selects_tests() -> None:
    """Rename detection is OFF in the reviewed diff (`git diff base...head`, no `-M`), so a move
    arrives as delete-in-A + add-in-B and the added name is new to B."""
    diff = _diff("src/rebar/old.py", [], ["def compute(x):"]) + _diff(
        "src/rebar/new.py", ["def compute(x):"]
    )
    assert _selects_tests(diff, ["src/rebar/old.py", "src/rebar/new.py"])


# ── the negative half: keyed on changed_files, not on the diff text ──────────────────────────
def test_a_changed_test_file_suppresses_the_content_trigger() -> None:
    diff = _diff("src/rebar/thing.py", ["def compute(x):"]) + _diff(
        "tests/unit/test_thing.py", ["def test_compute():"]
    )
    assert not _selects_tests(diff, ["src/rebar/thing.py", "tests/unit/test_thing.py"])


def test_glob_trigger_still_fires_when_a_test_file_changes() -> None:
    """Union, not replacement: the change above still gets the overlay, via the glob path."""
    assert "tests" in registry.glob_triggered_overlays(
        ["src/rebar/thing.py", "tests/unit/test_thing.py"]
    )


def test_docs_only_diff_with_a_fenced_declaration_does_not_select() -> None:
    diff = _diff("docs/guide.md", ["```python", "def f():", "    ...", "```"])
    assert not _selects_tests(diff, ["docs/guide.md"])


def test_config_only_diff_does_not_select() -> None:
    diff = _diff("pyproject.toml", ['name = "rebar"'])
    assert not _selects_tests(diff, ["pyproject.toml"])


def test_test_file_only_diff_does_not_content_select() -> None:
    diff = _diff("tests/unit/test_thing.py", ["def test_compute():"])
    assert not _selects_tests(diff, ["tests/unit/test_thing.py"])


def test_go_receiver_method_is_named_by_its_method_not_its_receiver() -> None:
    """`func (s *Store) Compute(` declares `Compute`; renaming only the receiver adds nothing."""
    diff = _diff(
        "internal/store/db.go",
        ["func (st *Store) Compute(x int) int {"],
        ["func (s *Store) Compute(x int) int {"],
    )
    assert not _selects_tests(diff, ["internal/store/db.go"])


def test_anonymous_declaration_yields_no_name_and_does_not_select() -> None:
    """A nameless declaration cannot take part in the removed-name comparison, so it is skipped —
    the trigger fails closed rather than firing on a rewrite it cannot reason about."""
    diff = _diff("app/handler.ts", ["export default function (req, res) {"])
    assert not _selects_tests(diff, ["app/handler.ts"])


def test_step_description_names_the_added_declaration_trigger() -> None:
    """The registered step's own description is the operator-facing contract for what fires."""
    from rebar.llm.code_review import workflow_ops  # noqa: F401  (registers the step)
    from rebar.llm.workflow.executor import contract_for

    description = contract_for("overlay_union").description
    assert "ADDS a declaration" in description
    assert "deletion-impact on a" in description


# ── portability: no language and no repository-layout assumption ─────────────────────────────
@pytest.mark.parametrize(
    ("path", "line"),
    [
        ("internal/store/db.go", "func Compute(x int) int {"),
        ("internal/store/db.go", "func (s *Store) Compute(x int) int {"),
        ("app/lib/compute.ts", "export function compute(x: number) {"),
        ("lib/rebar/compute.rb", "class Compute"),
        ("Sources/App/Compute.swift", "class Compute {"),
    ],
)
def test_added_declaration_outside_python_and_outside_src_selects(path: str, line: str) -> None:
    assert _selects_tests(_diff(path, [line]), [path])


# ── replay of the cited corpus shapes ────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("case", "diff", "changed", "expected"),
    [
        pytest.param(
            "d9e0f0e733 new module, 15 added declarations",
            _diff(
                "src/rebar/_engine/rebar_reconciler/git_adapter.py",
                [f"def helper_{i}(self):" for i in range(15)],
            ),
            ["src/rebar/_engine/rebar_reconciler/git_adapter.py"],
            True,
            id="d9e0f0e733",
        ),
        pytest.param(
            "721cb7d6ac moved declarations",
            _diff("src/rebar/_store/old.py", [], [f"def moved_{i}():" for i in range(3)])
            + _diff("src/rebar/_store/gitutil.py", [f"def moved_{i}():" for i in range(3)]),
            ["src/rebar/_store/old.py", "src/rebar/_store/gitutil.py"],
            True,
            id="721cb7d6ac",
        ),
        pytest.param(
            "1c2bf6e57b module split",
            _diff("src/rebar/_lib_identity.py", [f"def id_{i}():" for i in range(9)])
            + _diff("src/rebar/_lib_mutations.py", [f"def mut_{i}():" for i in range(13)]),
            ["src/rebar/_lib_identity.py", "src/rebar/_lib_mutations.py"],
            True,
            id="1c2bf6e57b",
        ),
        pytest.param(
            "24917e843b comment-only source delta",
            _diff("src/rebar/attest/x.py", ["    # explain the invariant"])
            + _diff("docs/architecture.md", ["A paragraph."]),
            ["src/rebar/attest/x.py", "docs/architecture.md"],
            False,
            id="24917e843b",
        ),
        pytest.param(
            "83d91d488c annotation-only signature rewrite",
            _diff(
                "src/rebar/attest/authorship.py",
                [f"def sig_{i}(x: str | None = None) -> str | None:" for i in range(8)],
                [f"def sig_{i}(x=None) -> str | None:" for i in range(8)],
            ),
            ["src/rebar/attest/authorship.py"],
            False,
            id="83d91d488c",
        ),
    ],
)
def test_corpus_replay(case: str, diff: str, changed: list[str], expected: bool) -> None:
    assert _selects_tests(diff, changed) is expected, case


# ── the prompt half of the story ─────────────────────────────────────────────────────────────
def test_guard_vii_defines_newly_introduced_in_diff_decidable_terms() -> None:
    body = prompts.get_prompt("code-review-tests").text
    assert "newly introduced" in body
    assert "pre-existing" in body
    # the distinction must be decidable from the diff alone (no trigger provenance reaches here)
    assert "ADDED (`+`) lines" in body or "added (`+`) lines" in body
    assert "named violating input" in body


def test_four_criterion_test_bullet_is_unchanged() -> None:
    assert "Four-Criterion Test" in prompts.get_prompt("code-review-tests").text


# ── back-compat: the committed content triggers are untouched ────────────────────────────────
def test_removed_declaration_still_fires_deletion_impact_without_changed_files() -> None:
    diff = _diff("src/rebar/thing.py", [], ["def gone(x):"])
    assert registry.content_triggered_overlays(diff) == ["deletion-impact"]


def test_positional_call_signature_still_works() -> None:
    diff = _diff("src/rebar/thing.py", [], ["def gone(x):"])
    assert registry.content_triggered_overlays(diff, None) == ["deletion-impact"]
