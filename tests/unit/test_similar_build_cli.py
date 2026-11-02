"""``rebar store build-vectors``: the explicit, opt-in similar-ticket vector build.

Ticket 0461-09ce-4850-42a7. The embedding tier landed with no way to populate it, so
the vectors it ranks on could only ever appear by calling the library directly. This
command is that missing surface, and the properties under test are the ones that make
it safe to ship a command which spends a user's provider budget:

* **Opt-in stays opt-in.** With no ``[llm] embedding_model`` configured the build is
  never entered, so an installation that has not asked for the tier cannot be billed
  by running the command, and ``rebar create`` cannot be billed by existing.
* **Not opting in is not an error.** An unconfigured install exits 0 and says why; a
  provider that will not answer exits 1 and says why. The build reports both as
  ``unavailable``, so the exit code is decided before the build runs rather than by
  matching on its reason text.
* **Incremental and resumable.** A second run over a covered corpus re-embeds nothing,
  and ``--limit`` leaves a measured remainder rather than silently truncating.

The embedding provider is injected through the one seam
``rebar.similar.embedding._embedding_model``, exactly as the tier's own tests do, so
none of this binds to a vendor.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from test_similar_tickets_embedding import (
    TestEmbeddingModel,
    _configure,
    _digest_json,
    _unconfigure,
    _write_ticket,
)

from rebar.similar._cli import build_vectors_cli

pytestmark = pytest.mark.unit

_HELP = Path(__file__).resolve().parents[2] / "src" / "rebar" / "_cli" / "help" / "store.txt"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A repo root whose store is the tracker directory the handler will resolve."""
    root = tmp_path / "repo"
    (root / ".tickets-tracker").mkdir(parents=True)
    monkeypatch.setenv("REBAR_TRACKER_DIR", str(root / ".tickets-tracker"))
    return root


def _tracker(repo: Path) -> Path:
    return repo / ".tickets-tracker"


def _explode(*_args: Any, **_kwargs: Any) -> Any:
    """An embedding seam that fails the test if the build ever reaches a provider."""
    raise AssertionError("the provider was called")


def _install(monkeypatch: pytest.MonkeyPatch, vocabulary: tuple[str, ...]) -> TestEmbeddingModel:
    model = TestEmbeddingModel(vocabulary)
    monkeypatch.setattr("rebar.similar.embedding._embedding_model", lambda _name: model)
    monkeypatch.setattr(
        "rebar.similar.embedding._digest_reply",
        lambda _name, text: _digest_json(propositions=["throttle"]) if text else "{}",
    )
    return model


# --- the command is reachable --------------------------------------------------------


def test_the_member_help_announces_its_own_prog(capsys: pytest.CaptureFixture[str]) -> None:
    """AC1. ``--help`` is how an operator discovers the command, so it must self-name.

    Driven through the family dispatcher rather than the handler, because that is the
    surface an operator reaches: ``rebar store <member> --help`` is intercepted before
    the handler runs, and it is the dispatcher that capitalizes the usage prefix.
    """
    from rebar._cli._store_commands import store_cli

    assert store_cli(["build-vectors", "--help"]) == 0
    assert capsys.readouterr().out.startswith("Usage: rebar store build-vectors")


def test_the_family_help_lists_the_member() -> None:
    """AC2. A command absent from ``rebar store --help`` is a command nobody finds."""
    help_text = _HELP.read_text(encoding="utf-8")
    assert "build-vectors" in help_text.split("commands:", 1)[1]


# --- opt-in stays opt-in -------------------------------------------------------------


def test_an_unconfigured_install_exits_zero_without_calling_a_provider(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC3. Not having opted in is a state to report, not a failure -- and not a bill."""
    _unconfigure(monkeypatch)
    monkeypatch.setattr("rebar.similar.embedding._embedding_model", _explode)
    _write_ticket(_tracker(repo), "1111-1111-1111-1111", "quota handling", "throttle detail")

    assert build_vectors_cli([], repo_root=str(repo)) == 0
    assert "embedding_model" in capsys.readouterr().out


@pytest.fixture
def initialized_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A git repo with a real initialized store, as ``rebar create`` requires."""
    import subprocess

    import rebar

    root = tmp_path / "created"
    root.mkdir()
    for args in (
        ("init", "-q"),
        ("config", "user.email", "test@example.com"),
        ("config", "user.name", "Test"),
    ):
        subprocess.run(["git", *args], cwd=root, check=True)
    monkeypatch.delenv("REBAR_TRACKER_DIR", raising=False)
    monkeypatch.setenv("REBAR_ROOT", str(root))
    rebar.init_repo(repo_root=str(root))
    return root


def test_create_makes_no_provider_call_with_the_duplicate_probe_enabled(
    initialized_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC8. The create path reaches the tier and must still spend nothing.

    Enabling ``verify.suggest_duplicate_tickets`` is what puts a create in contact with
    the similar-ticket tier at all; with it off the assertion would hold for the
    uninteresting reason that no retrieval happens. Both models are configured, so a
    create that wanted to embed could. The index file is asserted to exist as the
    witness that the path genuinely ran -- without it, zero provider calls would prove
    only that the feature was switched off.
    """
    import rebar

    _configure(monkeypatch)
    monkeypatch.setenv("REBAR_VERIFY_SUGGEST_DUPLICATE_TICKETS", "1")
    model = TestEmbeddingModel(("throttle",))
    digest_calls: list[str] = []
    monkeypatch.setattr("rebar.similar.embedding._embedding_model", lambda _name: model)
    monkeypatch.setattr(
        "rebar.similar.embedding._digest_reply",
        lambda _name, text: digest_calls.append(text) or _digest_json(propositions=["throttle"]),
    )

    assert rebar.create_ticket(
        "task", "quota handling", description="throttle detail", repo_root=str(initialized_repo)
    )
    index = initialized_repo / ".rebar" / "cache" / "similar-tickets" / "index.sqlite3"
    assert index.exists(), "the create never reached the similar-ticket tier"
    assert model.calls == []
    assert digest_calls == []


# --- the build itself ----------------------------------------------------------------


def test_a_first_run_vectors_the_corpus_and_a_second_run_re_embeds_nothing(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC4. The build is incremental: coverage is the state, not the act of running."""
    tracker = _tracker(repo)
    _write_ticket(tracker, "1111-1111-1111-1111", "quota handling", "throttle detail")
    _write_ticket(tracker, "2222-2222-2222-2222", "retry policy", "throttle too")
    _configure(monkeypatch)
    _install(monkeypatch, ("throttle",))

    assert build_vectors_cli(["--output", "json"], repo_root=str(repo)) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["vectored"] == 2
    assert first["status"] == "complete"

    assert build_vectors_cli(["--output", "json"], repo_root=str(repo)) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["vectored"] == 0


def test_a_limit_leaves_a_measured_remainder(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC5. A budgeted run must say what it did NOT reach, or it cannot be resumed."""
    tracker = _tracker(repo)
    _write_ticket(tracker, "1111-1111-1111-1111", "quota handling", "throttle detail")
    _write_ticket(tracker, "2222-2222-2222-2222", "retry policy", "throttle too")
    _configure(monkeypatch)
    _install(monkeypatch, ("throttle",))

    assert build_vectors_cli(["--limit", "1", "--output", "json"], repo_root=str(repo)) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "partial"
    assert report["remaining"] >= 1


@pytest.mark.parametrize("limit", ["0", "-1"])
def test_a_non_positive_limit_is_refused_before_any_provider_call(
    limit: str, repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A budget of zero or less is a caller mistake, not a run that embeds nothing.

    The guard matters because the configured path is reached first otherwise: a limit
    that argparse accepts but the build cannot honour would either bill a provider for
    a run the caller did not ask for or report a success it did not perform. Refusing
    with exit 2 keeps a malformed budget distinguishable from both an unconfigured
    install (0) and a provider that refused (1).
    """
    tracker = _tracker(repo)
    _write_ticket(tracker, "1111-1111-1111-1111", "quota handling", "throttle detail")
    _configure(monkeypatch)
    model = _install(monkeypatch, ("throttle",))

    assert build_vectors_cli(["--limit", limit], repo_root=str(repo)) == 2
    captured = capsys.readouterr()
    assert "must be 1 or greater" in captured.err
    assert model.calls == []


def test_json_output_is_one_object_carrying_every_counter(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC6. The machine-readable form is the one a wrapper parses; it must be total."""
    _write_ticket(_tracker(repo), "1111-1111-1111-1111", "quota handling", "throttle detail")
    _configure(monkeypatch)
    _install(monkeypatch, ("throttle",))

    assert build_vectors_cli(["--output", "json"], repo_root=str(repo)) == 0
    out = capsys.readouterr().out
    assert out.count("\n") == 1
    report = json.loads(out)
    assert set(report) >= {
        "status",
        "vectored",
        "skipped",
        "empties",
        "digest_unparsed",
        "digest_failed",
    }


def test_a_provider_that_raises_exits_one_carrying_the_reason(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """AC7. A build that cannot proceed must not look like one that found nothing to do."""
    _write_ticket(_tracker(repo), "1111-1111-1111-1111", "quota handling", "throttle detail")
    _configure(monkeypatch)
    _install(monkeypatch, ("throttle",))

    def _refuse(*_args: Any, **_kwargs: Any) -> list[list[float]]:
        raise PermissionError("denied by the endpoint")

    monkeypatch.setattr("rebar.similar.embedding._embed", _refuse)

    assert build_vectors_cli([], repo_root=str(repo)) == 1
    captured = capsys.readouterr()
    assert "denied by the endpoint" in captured.err
