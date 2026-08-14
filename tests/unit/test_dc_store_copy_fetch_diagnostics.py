"""Repo-tier oracles for the DC store-copy fixture's git diagnostics (``ancient-domestic-orca``).

WHY THIS LIVES IN THE UNIT TIER. The defect it pins was found in a Live External Integration
run: ``dc_store_copy_repo`` aborted at setup with ``CalledProcessError: Command '['git',
'fetch','origin','tickets']' returned non-zero exit status 128`` and nothing else. The call was
``subprocess.run(..., capture_output=True, check=True)``, so git's stderr — the one line naming
the actual cause — went into a captured buffer that ``CalledProcessError``'s string form never
reports. A fix for "the log does not say why" is only verifiable by showing the message DOES
say why, and a demonstration that needs a booted amd64 harness image and 41 minutes of live
spend is a demonstration nobody repeats. The two helpers carrying the repair are plain
module-level functions, so they are driven here with a stubbed ``subprocess.run`` on every
commit.

The live lane is not a gate for this ticket: it reads the improved message on its next
scheduled run.
"""

from __future__ import annotations

import ast
import importlib.util
import subprocess
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

_FIXTURES = Path(__file__).resolve().parents[1] / "external" / "live_jira_dc" / "_dc_fixtures.py"

# A distinctive line, of the shape git actually emits for the failure family under
# investigation. The assertions demand it VERBATIM: a message that merely says "fetch failed"
# would pass a laxer check while leaving the operator exactly as stuck as before.
GIT_STDERR = "fatal: couldn't find remote ref tickets"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_dc_fixtures_orca", _FIXTURES)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def dc_fixtures() -> ModuleType:
    return _load()


def _fail(stderr: str = GIT_STDERR, returncode: int = 128) -> Any:
    def runner(argv: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(returncode=returncode, stdout=b"", stderr=stderr.encode())

    return runner


def _ok(stdout: bytes = b"") -> Any:
    def runner(argv: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(returncode=0, stdout=stdout, stderr=b"")

    return runner


class TestRunGitSurfacesStderr:
    """The diagnostic repair: a failure names WHY, not just an exit status."""

    def test_failure_message_carries_git_stderr_command_and_status(
        self, dc_fixtures: ModuleType, tmp_path: Path
    ) -> None:
        with pytest.raises(RuntimeError) as excinfo:
            dc_fixtures.run_git(["git", "fetch", "origin", "tickets"], cwd=tmp_path, runner=_fail())

        message = str(excinfo.value)
        # git's own words, verbatim — the thing the old shape discarded.
        assert GIT_STDERR in message
        # the remote and the branch, so the message identifies WHICH fetch died.
        assert "origin" in message and "tickets" in message
        assert "128" in message

    def test_old_shape_is_the_negative_control(self, tmp_path: Path) -> None:
        """RED-proof: the discarded-stderr shape this ticket replaced fails the assertion above.

        Without this control the cell above is a change-detector — it would pass against any
        error type that happened to mention the branch name. ``CalledProcessError`` is
        constructed exactly as ``check=True`` constructs it, stderr and all, and its string
        form still does not contain that stderr.
        """
        error = subprocess.CalledProcessError(
            128, ["git", "fetch", "origin", "tickets"], output=b"", stderr=GIT_STDERR.encode()
        )
        assert GIT_STDERR not in str(error)

    def test_missing_stderr_still_produces_a_readable_message(
        self, dc_fixtures: ModuleType, tmp_path: Path
    ) -> None:
        with pytest.raises(RuntimeError, match="<git wrote no stderr>"):
            dc_fixtures.run_git(["git", "archive", "FETCH_HEAD"], cwd=tmp_path, runner=_fail(""))

    def test_success_returns_the_completed_process(
        self, dc_fixtures: ModuleType, tmp_path: Path
    ) -> None:
        result = dc_fixtures.run_git(
            ["git", "archive", "FETCH_HEAD"], cwd=tmp_path, runner=_ok(b"TARBYTES")
        )
        assert result.stdout == b"TARBYTES"


class TestFetchTicketsRetries:
    """The hardening: one blip on a 41-minute live job must not ERROR the cell."""

    def test_transient_failure_is_retried_then_succeeds(
        self, dc_fixtures: ModuleType, tmp_path: Path
    ) -> None:
        calls: list[Any] = []
        slept: list[float] = []

        def runner(argv: Any, **kwargs: Any) -> Any:
            calls.append(argv)
            if len(calls) == 1:
                return SimpleNamespace(returncode=128, stdout=b"", stderr=GIT_STDERR.encode())
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

        result = dc_fixtures.fetch_tickets(tmp_path, runner=runner, sleep=slept.append)

        assert result.returncode == 0
        assert len(calls) == 2
        assert calls[1] == ["git", "fetch", "origin", "tickets"]
        assert slept, "a retry must back off rather than hammer the remote"

    def test_persistent_failure_is_bounded_and_still_names_git_stderr(
        self, dc_fixtures: ModuleType, tmp_path: Path
    ) -> None:
        calls: list[Any] = []

        def runner(argv: Any, **kwargs: Any) -> Any:
            calls.append(argv)
            return SimpleNamespace(returncode=128, stdout=b"", stderr=GIT_STDERR.encode())

        with pytest.raises(RuntimeError) as excinfo:
            dc_fixtures.fetch_tickets(tmp_path, runner=runner, sleep=lambda _s: None)

        # retry-then-FAIL, and the attempt count is capped rather than open-ended.
        assert len(calls) == dc_fixtures.FETCH_ATTEMPTS
        assert GIT_STDERR in str(excinfo.value)


def test_store_copy_fixture_leaves_no_swallowed_diagnostic(dc_fixtures: ModuleType) -> None:
    """No ``subprocess.run`` in the fixture module pairs ``capture_output`` with ``check``.

    Pinned structurally, on the parsed call sites rather than the text, because the defect is
    a call SHAPE: a future edit that reintroduces the pair would restore the undiagnosable
    failure without breaking any behavioural assertion here. Parsing (not grepping) keeps the
    prose in this module's own docstrings — which necessarily quote the bad shape — out of it.
    """
    tree = ast.parse(_FIXTURES.read_text())
    offenders = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and ast.unparse(node.func).endswith("subprocess.run")
        and {kw.arg for kw in node.keywords} >= {"capture_output", "check"}
    ]
    assert offenders == [], f"swallowed-diagnostic call shape at lines {offenders}"
    assert dc_fixtures.FETCH_ATTEMPTS >= 2


def _init_tickets_repo(tracker: Path) -> None:
    """A minimal `tickets`-branch store repo with one committed entry.

    Enough for :func:`seed_projects_mapping_unit`'s tree-check + commit and for a
    ``git status``/``git show`` assertion; NOT a full rebar store.
    """
    tracker.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "tickets"], cwd=tracker, check=True)
    subprocess.run(["git", "config", "user.email", "u@example.invalid"], cwd=tracker, check=True)
    subprocess.run(["git", "config", "user.name", "unit"], cwd=tracker, check=True)
    (tracker / "aaaa-bbbb-cccc-dddd").mkdir()
    (tracker / "aaaa-bbbb-cccc-dddd" / "events.jsonl").write_text("{}\n")
    subprocess.run(["git", "add", "-A"], cwd=tracker, check=True)
    subprocess.run(["git", "commit", "-q", "--no-verify", "-m", "seed"], cwd=tracker, check=True)


class TestBridgeStateScrub:
    """The J11 store-copy scrub must leave NO `.bridge_state` — even after `run_ensures`
    re-seeds it (bug 91aa / glass-worriless-mammal)."""

    def test_scrub_removes_bridge_state_by_glob(
        self, dc_fixtures: ModuleType, tmp_path: Path
    ) -> None:
        tracker = tmp_path / "t"
        (tracker / ".bridge_state" / "bridge_alerts").mkdir(parents=True)
        (tracker / ".bridge_state" / "projects.json").write_text("{}")
        # a renamed sibling: caught by the GLOB, not an enumerated exact name.
        (tracker / ".bridge_state.bak").write_text("x")
        # a NESTED artifact: caught only because the sweep is recursive (rglob), matching
        # the isolation cell's own `tracker.rglob('.bridge_state*')` assertion.
        (tracker / "sub").mkdir()
        (tracker / "sub" / ".bridge_state.tmp").write_text("y")
        (tracker / "keep").mkdir()

        removed = dc_fixtures.scrub_bridge_state(tracker)

        assert sorted(removed) == [".bridge_state", ".bridge_state.bak", ".bridge_state.tmp"]
        assert list(tracker.rglob(".bridge_state*")) == []
        assert (tracker / "keep").exists(), "the scrub must only touch `.bridge_state*`"
        assert (tracker / "sub").exists(), "the scrub must not remove the nested artifact's parent"

    def test_seed_unit_resurrects_the_cache_and_recommit_scrub_removes_it(
        self, dc_fixtures: ModuleType, tmp_path: Path
    ) -> None:
        """Regression origin + fix in one cell.

        The `projects-seed` ensure unit (ticket 462d / epic 0303) re-creates and COMMITS
        `.bridge_state/projects.json` after the first scrub deleted the blob its tree-check
        looks for — that resurrection is why the store copy carried `.bridge_state`. The
        second, committing scrub is the fix; asserting the seed FIRST is the negative control
        that keeps this from passing vacuously.
        """
        from rebar._store.project_ensures import seed_projects_mapping_unit

        tracker = tmp_path / "store"
        _init_tickets_repo(tracker)

        # NEGATIVE CONTROL: convergence resurrects the cache the scrub had removed.
        outcome = seed_projects_mapping_unit(str(tracker))
        assert outcome.status == "changed"
        assert (tracker / ".bridge_state" / "projects.json").is_file()

        # THE FIX: re-scrub after converge, committing the removal.
        removed = dc_fixtures.scrub_bridge_state(tracker, commit=True)

        assert ".bridge_state" in removed
        assert list(tracker.glob(".bridge_state*")) == [], (
            "`.bridge_state` survived the post-converge scrub"
        )
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=tracker, capture_output=True, text=True
        )
        assert status.stdout.strip() == "", "the re-scrub must commit its deletion"
        show = subprocess.run(
            ["git", "show", "tickets:.bridge_state/projects.json"],
            cwd=tracker,
            capture_output=True,
            text=True,
        )
        assert show.returncode != 0, "the seeded blob must be gone from the tickets tree too"
