"""The e2e browser tier may not skip silently — bug 337e-b558-17a2-49bd.

The tier drives the real editor bundle in headless Chromium and does not run in automated
builds. That is a deliberate decision (the browser download is a large cost on every run, and
change 2600 deliberately took the optional Playwright dependency off selections that contain
no browser test). What was NOT acceptable is the way the decision was expressed: a bare
``pytest.skip`` in the fixture, which reports the same green as a tier that actually ran.

These tests pin the two halves of the fix. The DECISION is a record in the tree
(``tests/e2e/browser-tier-optout.toml``); the ENFORCEMENT is that a non-execution consults it
and FAILS when it is absent, so removing the marker turns the tier red rather than quiet. They
need neither Node nor a browser, so they run on every cell of every lane — which is the point:
the check on the tier's absence must not itself be absent.
"""

from __future__ import annotations

import ast
import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
E2E_DIR = REPO_ROOT / "tests" / "e2e"
GUARD_PATH = E2E_DIR / "_browser_tier.py"
RECORD_PATH = E2E_DIR / "browser-tier-optout.toml"

#: The one e2e fixture that may still call ``pytest.skip`` directly: it gates the bpmn-moddle
#: ROUND-TRIP tier, which is a different tier with a different disposition. Every browser
#: fixture must route through the guard instead.
_SKIP_ALLOWED_IN = frozenset({"bpmn_harness"})


def _load_guard() -> ModuleType:
    """Import ``tests/e2e/_browser_tier.py`` by path (``tests/e2e`` is not a package)."""
    spec = importlib.util.spec_from_file_location("_browser_tier_under_test", GUARD_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before exec: ``@dataclass`` resolves its own module out of ``sys.modules``.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


guard = _load_guard()


# ── The decision is recorded in the tree (AC1) ────────────────────────────────


def test_committed_record_is_complete():
    """The shipped opt-out parses and states scope, deciding ticket and a real reason."""
    record = guard.load_record()
    assert record is not None, f"{RECORD_PATH} is missing or incomplete"
    assert record.scope == "local-only"
    assert record.decided_by == "337e-b558-17a2-49bd"
    # A reason is the whole point of the record: a future reader must learn WHY, not just THAT.
    assert len(record.reason.split()) >= 20, "the recorded reason is too thin to inform anyone"


# ── A licensed non-execution is loud, and says so (AC4) ───────────────────────


def test_skip_reason_is_loud_and_names_the_record():
    with pytest.raises(pytest.skip.Exception) as excinfo:
        guard.tier_unavailable("Chromium will not launch")
    message = str(excinfo.value)
    assert message.startswith(guard.NOT_RUN_BANNER)
    assert "Chromium will not launch" in message, "the concrete cause must survive into the reason"
    assert "337e-b558-17a2-49bd" in message, "the reason must name the deciding ticket"
    assert RECORD_PATH.name in message, "the reason must name the record a reader can go read"
    assert "not a pass" in message


def test_a_licensed_non_execution_warns_so_the_summary_shows_it():
    """pytest prints a warnings summary even under ``-q``; that is how a reader of a green
    run learns the tier did not execute."""
    with pytest.warns(guard.WARNING_CATEGORY, match=guard.NOT_RUN_BANNER):
        with pytest.raises(pytest.skip.Exception):
            guard.tier_unavailable("playwright is not installed")


def test_the_warning_category_is_importable_by_the_xdist_controller():
    """The announcement must not be able to kill the run it is announcing into.

    An xdist worker serializes a warning by its CLASS's module name and the controller
    re-imports that module to rebuild it. A category defined in ``tests/e2e/_browser_tier.py``
    is importable on the worker (pytest puts ``tests/e2e`` on its path) and NOT on the
    controller, which raises ``ModuleNotFoundError``, marks the node down and ends the whole
    session in an ``INTERNALERROR`` — the tier's loud absence taking the suite with it. So the
    category must come from a module any interpreter can import.

    Checked the way the controller does it: an ISOLATED interpreter (``-I`` ignores the
    current directory and ``PYTHONPATH``, exactly like the controller's own path) must be able
    to import the module the category is defined in.
    """
    module = guard.WARNING_CATEGORY.__module__
    probe = subprocess.run(
        [sys.executable, "-I", "-c", f"import importlib; importlib.import_module({module!r})"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert probe.returncode == 0, (
        f"the browser-tier warning category lives in {module!r}, which an xdist controller "
        f"cannot import; it must come from the installed environment (pytest, stdlib), not "
        f"from a tests/ module: {probe.stderr.strip()}"
    )


# ── Remove the marker and the tier turns RED (AC3) ────────────────────────────


def test_absent_record_fails_instead_of_skipping(tmp_path):
    """The anti-vacuity criterion: with nothing licensing it, a non-execution is a FAILURE.

    This is the automated form of "delete the record and observe RED". If this ever starts
    raising ``Skipped``, the marker has become decorative and the original defect is back.
    """
    outcome: BaseException | None = None
    try:
        guard.tier_unavailable("Chromium will not launch", path=tmp_path / "no-such-record.toml")
    except (pytest.skip.Exception, pytest.fail.Exception) as raised:
        outcome = raised

    assert outcome is not None, "tier_unavailable returned instead of ending the test"
    assert not isinstance(outcome, pytest.skip.Exception), (
        f"a non-execution with no recorded opt-out must FAIL, not skip; it skipped: {outcome}"
    )
    assert isinstance(outcome, pytest.fail.Exception)
    assert "SILENTLY SKIPPED" in str(outcome)
    # The cause and the file to restore both survive into the failure a reader will see.
    assert "Chromium will not launch" in str(outcome)
    assert "no-such-record.toml" in str(outcome)


@pytest.mark.parametrize(
    ("name", "body"),
    [
        ("empty", ""),
        ("wrong-table", '[browser]\nscope = "local-only"\n'),
        ("blank-reason", '[browser_tier]\nscope = "x"\ndecided_by = "y"\nreason = "  "\n'),
        ("missing-reason", '[browser_tier]\nscope = "x"\ndecided_by = "y"\n'),
        ("not-toml", "this is not toml = = =\n"),
    ],
)
def test_an_incomplete_record_licenses_nothing(tmp_path, name, body):
    """A half-written record is not a decision, so it must not buy a skip either."""
    path = tmp_path / f"{name}.toml"
    path.write_text(body, encoding="utf-8")
    assert guard.load_record(path) is None
    with pytest.raises(pytest.fail.Exception):
        guard.tier_unavailable("Chromium will not launch", path=path)


# ── Portability: the guard reads a file, not an environment (AC5) ─────────────


def test_guard_reads_no_environment_and_knows_no_ci_provider():
    """A checkout with no CI provider must behave identically to one with any.

    Enforced structurally rather than by inspection: the guard may not reach for the
    environment, and may not name a CI product, so there is nowhere for provider-specific
    behaviour to hide.
    """
    source = GUARD_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert "os" not in imported, "the guard must not consult the process environment"
    lowered = source.lower()
    for provider in ("github", "gitlab", "jenkins", "circleci", "buildkite", "travis", "azure"):
        assert provider not in lowered, f"the guard must not know about {provider}"
    # `CI` itself, the de-facto provider flag, must not be read either.
    assert '"ci"' not in lowered and "'ci'" not in lowered


# ── No unguarded path may be reintroduced ─────────────────────────────────────


def _functions(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            yield node


def test_no_browser_fixture_falls_back_to_a_bare_skip():
    """Every e2e fixture except the round-trip one must route non-execution through the guard.

    Without this scan the fix is one careless edit from regressing: a new fixture (or a new
    branch of an existing one) could add a bare ``pytest.skip`` beside the guarded calls and
    reopen exactly the silent path this ticket closed.
    """
    offenders = []
    for path in sorted(E2E_DIR.rglob("*.py")):
        if path.name in ("_browser_tier.py", "_toolchain.py"):
            # The guard itself is the ONE sanctioned `pytest.skip` call site; the
            # provisioning helper raises rather than skipping.
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for func in _functions(tree):
            if func.name in _SKIP_ALLOWED_IN:
                continue
            for node in ast.walk(func):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "skip"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "pytest"
                ):
                    rel = path.relative_to(REPO_ROOT).as_posix()
                    offenders.append(f"{rel}::{func.name}:{node.lineno}")
    assert offenders == [], (
        "these e2e functions skip without consulting the recorded opt-out; call "
        "`_browser_tier.tier_unavailable(...)` instead: " + ", ".join(offenders)
    )


def test_no_e2e_module_hides_a_test_behind_a_bare_skipif():
    """The other shape a silent non-execution can take, closed before it is used.

    A ``@pytest.mark.skipif`` on a browser test would drop it from the run just as invisibly
    as a bare ``pytest.skip``, and the fixture guard would never be reached to say so. There
    are none today; this keeps it that way, so the tier's disposition stays expressed in one
    place — the recorded opt-out — rather than in scattered decorators.
    """
    offenders = []
    for path in sorted(E2E_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "skipif":
                rel = path.relative_to(REPO_ROOT).as_posix()
                offenders.append(f"{rel}:{node.lineno}")
    assert offenders == [], (
        "a browser test dropped by `skipif` never reaches the guard, so its absence goes "
        "unannounced; express the disposition in browser-tier-optout.toml instead: "
        + ", ".join(offenders)
    )
