"""Offline contract tests for the project-owned `project.symbol-reference-completeness`
criterion and its Serena coaching move (story surprised-overjoyful-earthworm, epic
frail-tsarist-trout).

Exercise the REAL committed `.rebar/` overlay at the repo root: the routing entry, the
criterion prompt, and the Pass-4 move — no model call, no network.

The failure modes these guard are all SILENT. A criterion routed to the wrong gate, a move
whose ``applies_when`` names a tag no finding ever carries, or an id that leaked into
`src/rebar` would each leave the files present and plausible while the coaching never fires
(or fires for every other rebar client).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from rebar.llm.criteria.ids import criterion_prompt_id
from rebar.llm.criteria.model import threshold_for
from rebar.llm.plan_review import coach_moves, registry
from rebar.llm.review_kernel.coach import applicable_moves

REPO_ROOT = Path(__file__).resolve().parents[2]
CRITERION_ID = "project.symbol-reference-completeness"
MOVE_ID = "project-symbol-reference-completeness"
_ROUTING = REPO_ROOT / ".rebar" / "criteria_routing.json"
_PROMPT = REPO_ROOT / ".rebar" / "prompts" / f"plan-review-{MOVE_ID}.md"


def _routing() -> dict:
    return json.loads(_ROUTING.read_text(encoding="utf-8"))


def _moves() -> dict:
    return coach_moves.load_move_registry(repo_root=str(REPO_ROOT))


# ── happy path ──────────────────────────────────────────────────────────────────────
def test_criterion_is_active_for_plan_review_with_a_coaching_move():
    """The criterion loads for plan-review and carries a move naming the Serena path."""
    assert CRITERION_ID in registry.effective_criteria(str(REPO_ROOT)), (
        f"{CRITERION_ID} is not in the effective plan-review criteria — check the routing "
        "entry and its 'activate' mapping"
    )
    move = _moves().get(MOVE_ID)
    assert move is not None, f"move {MOVE_ID!r} is not in the loaded registry"
    assert "find_referencing_symbols" in move["template"], (
        "the move must direct re-derivation through Serena's find_referencing_symbols; "
        f"template was: {move['template']!r}"
    )


# ── edge / contract ─────────────────────────────────────────────────────────────────
def test_move_also_directs_the_string_literal_sweep():
    """Half the advice is worse than none: the LSP cannot see string-named symbols."""
    template = _moves()[MOVE_ID]["template"]
    assert "monkeypatch.setattr" in template or "getattr" in template, (
        "the move must name the string-literal sweep (monkeypatch.setattr / getattr) that "
        f"find_referencing_symbols cannot resolve; template was: {template!r}"
    )


def test_move_renders_deterministically_with_a_subject():
    """The kernel substitutes {subject}; the LLM only picks the move and names the subject."""
    move = _moves()[MOVE_ID]
    assert "{subject}" in move["template"], "the template must carry a {subject} placeholder"
    rendered = move["template"].format(subject="the Scope section")
    assert "the Scope section" in rendered and "{subject}" not in rendered


@pytest.mark.parametrize("trigger", [CRITERION_ID, "G1G2", "G6"])
def test_move_is_offered_for_every_inventory_completeness_trigger(trigger: str):
    """Reachability, learned from live runs: the SAME incomplete-inventory evidence that fires
    this project criterion (advisory) also fires the built-in G1G2/G6 (blocking). Pass-4 coaches
    blocking findings FIRST, so a move keyed only on the advisory project id competes last and
    is never offered in practice — a dead move by construction. Keying it on the built-in
    inventory criteria too is what makes the Serena coaching actually reachable."""
    assert MOVE_ID in applicable_moves(_moves(), {trigger}), (
        f"the move is not offered for trigger {trigger!r} — it would be unreachable whenever a "
        "built-in inventory criterion blocks on the same evidence"
    )


@pytest.mark.parametrize("triggers", [set(), {"project.portability"}, {"security", "tests"}])
def test_move_is_not_offered_for_unrelated_triggers(triggers: set[str]):
    """The other half of the contract: a move offered on every review is noise, not coaching."""
    assert MOVE_ID not in applicable_moves(_moves(), triggers), (
        f"the move fired for {triggers or '(no triggers)'} — its applies_when is too broad "
        "(empty/'always' would offer it on every review)"
    )


def test_criterion_is_advisory_and_never_blocking():
    """This coaches a method, not a correctness property — a blocking posture gates process."""
    entry = _routing()["plan_review"][CRITERION_ID]
    assert entry.get("default_posture") == "advisory", (
        f"default_posture must be 'advisory', got {entry.get('default_posture')!r}"
    )
    _threshold, blocking = threshold_for(
        [CRITERION_ID], registry.effective_routing(str(REPO_ROOT)), gate="plan_review"
    )
    assert blocking is False, "the criterion resolves as BLOCKING for plan-review"


def test_routing_entry_carries_no_inert_blocking_enabled_key():
    """`blocking_enabled` is code-review's convention; plan-review's threshold_for never
    reads it, so shipping it here would look like a control and be a no-op."""
    entry = _routing()["plan_review"][CRITERION_ID]
    assert "blocking_enabled" not in entry, (
        "blocking_enabled is inert for plan-review (criteria/model.py resolves plan-review "
        "blocking from default_posture alone) — a reader would mistake it for the control"
    )


def test_criterion_routes_to_plan_review_only():
    """A `project.*` id can never enter code-review's closed applies_when vocabulary, so a
    code-review routing would register a permanently dead move."""
    routing = _routing()
    assert routing["activate"][CRITERION_ID] == ["plan_review"], (
        "activate must be exactly ['plan_review']; a code_review routing would be dead "
        "because code-review active_triggers are drawn from OVERLAY_IDS, not project ids"
    )
    assert CRITERION_ID not in routing.get("code_review", {})


def test_criterion_prompt_exists_at_the_conventional_path():
    """`project.<name>` maps to `plan-review-project-<name>.md`; a missing rubric means the
    criterion loads with no detection detail."""
    assert criterion_prompt_id(CRITERION_ID, gate_key="plan_review") == f"plan-review-{MOVE_ID}"
    assert _PROMPT.is_file(), f"criterion prompt missing at {_PROMPT}"
    body = _PROMPT.read_text(encoding="utf-8")
    assert "category: plan-review-criterion" in body, "prompt front-matter lacks the category"
    assert CRITERION_ID in body, (
        "the prompt must tell the reviewer to emit this criterion id in `criteria`, or the "
        "finding can never activate the move"
    )
    assert "monkeypatch.setattr" in body or "string" in body.lower(), (
        "the rubric must cover the string-literal class it is meant to catch"
    )


@pytest.mark.parametrize(
    "path",
    [
        ".rebar/criteria_routing.json",
        ".rebar/plan_review_moves.json",
        f".rebar/prompts/plan-review-{MOVE_ID}.md",
    ],
)
@pytest.mark.allow_unharnessed_subprocess(
    "asks git whether THIS checkout tracks the overlay asset; that is the assertion"
)
def test_every_overlay_asset_is_tracked_by_git(path: str):
    """`.rebar/` is ignored with per-file negations (`.gitignore` `.rebar/prompts/*`), so a
    new rubric is invisible to git by DEFAULT — it exists locally, the criterion loads for
    the author, and it ships to nobody."""
    proc = subprocess.run(
        ["git", "ls-files", "--error-unmatch", path],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"{path} is not tracked by git — add a `!{path}` negation to .gitignore, or the "
        f"criterion works only in this checkout (git said: {proc.stderr.strip()})"
    )


@pytest.mark.allow_unharnessed_subprocess(
    "greps the real committed src/rebar to prove the overlay id never shipped"
)
def test_criterion_id_is_absent_from_shipped_source():
    """The whole point of the overlay: other rebar clients' default criteria set is unchanged."""
    proc = subprocess.run(
        ["git", "grep", "-l", CRITERION_ID, "--", "src/rebar"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.stdout.strip() == "", (
        f"{CRITERION_ID} is hardcoded in shipped source ({proc.stdout.strip()}) — it must ride "
        "the .rebar/ project overlay only"
    )


def test_packaged_default_criteria_do_not_include_it():
    """Belt and braces on the same invariant, through the loader rather than the filesystem."""
    assert CRITERION_ID not in registry.effective_criteria(repo_root=None)


@pytest.mark.parametrize("gate_file", ["plan_review_moves.json", "code_review_moves.json"])
def test_move_is_declared_only_in_the_plan_review_move_file(gate_file: str):
    """Code-review moves live in a DIFFERENT file; putting it there would be dead weight."""
    path = REPO_ROOT / ".rebar" / gate_file
    present = path.is_file() and MOVE_ID in json.loads(path.read_text(encoding="utf-8"))
    assert present == (gate_file == "plan_review_moves.json"), (
        f"{MOVE_ID} presence in {gate_file} is wrong: expected "
        f"{gate_file == 'plan_review_moves.json'}, got {present}"
    )


def test_real_review_run_evidence_is_committed():
    """AC: the demonstration must be a repo-verifiable artifact, not only a ticket comment."""
    sample = REPO_ROOT / "docs" / "serena-symbol-reference-coaching-sample.md"
    assert sample.is_file(), f"missing committed review-run evidence at {sample}"
    body = sample.read_text(encoding="utf-8")
    assert CRITERION_ID in body and "review-plan" in body, (
        "the evidence file must show a real review-plan run naming the criterion"
    )
