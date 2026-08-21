"""RP-05 S3 — the registry-driven core CLI execution oracle.

After S2 the registry named every route and carried lazy parser factories, but
runtime *execution* still traversed hand-written ``if sub == ...`` ladders in
``rebar._cli`` with per-arm init policy and per-arm handler call shapes. S3 cuts
the core + bridge execution over so the selected registry route is the single
selection authority: each route carries a lazy handler reference, a bounded
``adapter`` kind (the exact call shape), an ``init`` policy, and — for
``bridge-status`` — an ``argv_prefix``. The top-level router resolves ONLY the
selected handler and invokes it through the closed adapter set; it never parses
the command remainder a second time.

These tests assert OBSERVABLE behavior only: the call shape a handler receives
(via typed fakes), the init policy applied (via a spy), the streams/exit codes of
``main()``, exception propagation, and — through a parser spy — that the selected
S2 factory parses exactly once while unrelated factories are never imported.

The happy-path block at the top is the only part handed to the implementer; the
adversarial adapter/init census, the parser spy, import isolation, exception
propagation, the ``--`` and malformed-flag controls, and the bridge vocabulary
are the held-out oracle validated by the orchestrator.
"""

from __future__ import annotations

import subprocess
import sys

import pytest
from _subprocess_env import subprocess_env

from rebar._cli import _execute, _registry, main

pytestmark = pytest.mark.unit


# Every route the core dispatcher (``_dispatch`` → ``_execute.execute``) owns is
# exactly a route that carries a lazy handler. After the RP-05 S6 cutover that includes
# the former intercept-ladder commands (reconcile, review-plan, config, audit, identity,
# ...): they now carry real execution metadata and route through the SAME executor — the
# separate intercept selection authority was retired.
def _dispatch_routes() -> tuple[_registry.Route, ...]:
    return tuple(r for r in _registry.ROUTES if r.handler is not None)


# ------------------------------------------------------------------ happy path


def test_dispatch_routes_carry_a_closed_adapter_and_init_policy() -> None:
    """Every executable route names a bounded adapter kind and a known init policy."""
    routes = _dispatch_routes()
    assert routes, "no routes carry a handler — the cutover did not happen"
    for route in routes:
        assert route.adapter in _registry.ADAPTER_KINDS, f"{route.name}: {route.adapter!r}"
        assert route.init in _registry.INIT_POLICIES, f"{route.name}: init {route.init!r}"


def test_reads_route_dispatches_through_the_dispatcher_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``list`` resolves the reads dispatcher and hands it ``[name, *rest]``."""
    seen: list[list[str]] = []
    monkeypatch.setattr("rebar._engine_support.reads.main", lambda argv=None: seen.append(argv))
    monkeypatch.setattr("rebar._cli.ensure_initialized", lambda **_k: None)

    _execute.execute("list", ["--status", "open"])

    assert seen == [["list", "--status", "open"]]


def test_argv_route_dispatches_the_bare_remainder(monkeypatch: pytest.MonkeyPatch) -> None:
    """A plain ``argv`` route (metrics) receives the remainder unchanged."""
    seen: list[list[str]] = []
    monkeypatch.setattr("rebar._commands.metrics.metrics_cli", lambda argv: seen.append(argv) or 0)
    monkeypatch.setattr("rebar._cli.ensure_initialized", lambda **_k: None)

    rc = _execute.execute("metrics", ["gate-economics", "--output", "json"])

    assert rc == 0
    assert seen == [["gate-economics", "--output", "json"]]


def test_execute_returns_the_handler_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("rebar._commands.metrics.metrics_cli", lambda argv: 7)
    monkeypatch.setattr("rebar._cli.ensure_initialized", lambda **_k: None)
    assert _execute.execute("metrics", []) == 7


# ------------------------------------------------------------------ held-out oracle
# Withheld from the implementer; validated post-hoc by the orchestrator.


def test_router_delegates_core_dispatch_to_the_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cutover itself: a non-intercept spelling routes through ``_execute.execute``."""
    seen: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        "rebar._cli._execute.execute", lambda name, rest: seen.append((name, rest)) or 0
    )
    monkeypatch.setattr("rebar._cli.ensure_store_mounted_best_effort", lambda: None)

    main(["list", "--status", "open"])

    assert seen == [("list", ["--status", "open"])], "router did not delegate to the executor"


# --- adapter-kind census: a typed fake per closed adapter kind ----------------


def _no_init(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("rebar._cli.ensure_initialized", lambda **_k: None)
    monkeypatch.setattr("rebar._engine_support.reads.tracker_dir", lambda: "/tmp/tk")


def test_dispatcher_adapter_prefixes_the_route_name_for_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr("rebar._commands.main", lambda argv: seen.append(argv) or 0)
    _no_init(monkeypatch)
    _execute.execute("comment", ["tid", "hello"])
    assert seen == [["comment", "tid", "hello"]]


def test_argv_tracker_adapter_appends_the_tracker(monkeypatch: pytest.MonkeyPatch) -> None:
    """``get-file-impact`` (argv_tracker) receives ``(rest, tracker_dir())``."""
    seen: list[tuple] = []
    monkeypatch.setattr(
        "rebar._engine_support.field_reads.file_impact_cli",
        lambda argv, tracker: seen.append((argv, tracker)) or 0,
    )
    _no_init(monkeypatch)
    _execute.execute("get-file-impact", ["abcd"])
    assert seen == [(["abcd"], "/tmp/tk")]


def test_argv_tracker_root_adapter_appends_tracker_and_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``format``/``clarity-check`` (argv_tracker_root) get ``(rest, tracker, dirname)``."""
    seen: list[tuple] = []
    monkeypatch.setattr(
        "rebar._engine_support.lookups.format_cli",
        lambda argv, tracker, root: seen.append((argv, tracker, root)) or 0,
    )
    monkeypatch.setattr("rebar._cli.ensure_initialized", lambda **_k: None)
    monkeypatch.setattr("rebar._engine_support.reads.tracker_dir", lambda: "/repo/.tickets-tracker")
    _execute.execute("format", ["abcd"])
    assert seen == [(["abcd"], "/repo/.tickets-tracker", "/repo")]


def test_every_adapter_kind_has_at_least_one_route() -> None:
    """The closed adapter set is exactly the kinds the route table actually uses."""
    used = {r.adapter for r in _dispatch_routes()}
    assert used == set(_registry.ADAPTER_KINDS)


# --- init-policy census: the pre-cutover per-arm policy, route by route --------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("show", "init_only"),
        ("list", "init_only"),
        ("session-logs", "init_only"),
        ("validate", "none"),
        ("comment", "full"),
        ("create", "full"),
        ("transition", "full"),
        ("claim", "full"),
        ("metrics", "init_only"),
        ("export", "init_only"),
        ("import", "full"),
        ("delete", "full"),
        ("fsck", "full"),
        ("tracker-maintenance", "full"),
        ("sign", "full"),
        ("get-file-impact", "full"),
        ("exists", "full"),
        ("list-descendants", "full"),
        ("clarity-check", "none"),
        ("check-ac", "none"),
        ("summary", "none"),
        ("init", "none"),
        ("scratch", "none"),
        ("bridge", "none"),
        ("bridge-status", "none"),
        ("bridge-fsck", "none"),
        ("bridge-probe", "none"),
        ("grounding-info", "none"),
    ],
)
def test_route_init_policy_matches_pre_cutover_census(name: str, expected: str) -> None:
    route = _registry.route_for(name)
    assert route is not None and route.init == expected


def _init_spy(monkeypatch: pytest.MonkeyPatch) -> list[bool | None]:
    """Record every ensure_initialized(init_only=...) call; None means 'not called'."""
    calls: list[bool] = []
    monkeypatch.setattr(
        "rebar._cli.ensure_initialized", lambda *, init_only: calls.append(init_only)
    )
    monkeypatch.setattr("rebar._engine_support.reads.tracker_dir", lambda: "/tmp/tk")
    return calls


def test_none_policy_never_initializes(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _init_spy(monkeypatch)
    monkeypatch.setattr("rebar._commands.init.init_cli", lambda argv: 0)
    _execute.execute("init", ["--help-ish"])
    assert calls == []


def test_init_only_policy_initializes_without_reconverge(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _init_spy(monkeypatch)
    monkeypatch.setattr("rebar._engine_support.reads.main", lambda argv=None: 0)
    _execute.execute("list", [])
    assert calls == [True]


def test_full_policy_initializes_with_reconverge(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _init_spy(monkeypatch)
    monkeypatch.setattr("rebar._commands.main", lambda argv: 0)
    _execute.execute("comment", ["tid", "x"])
    assert calls == [False]


def test_doctor_selector_is_conditional_on_repair(monkeypatch: pytest.MonkeyPatch) -> None:
    """``doctor`` inits read-only by default and full only under ``--repair``."""
    monkeypatch.setattr("rebar._commands.doctor.doctor_cli", lambda argv: 0)

    calls = _init_spy(monkeypatch)
    _execute.execute("doctor", [])
    assert calls == [True], "doctor without --repair must init read-only (init_only=True)"

    calls2 = _init_spy(monkeypatch)
    _execute.execute("doctor", ["--repair"])
    assert calls2 == [False], "doctor --repair must init full (init_only=False)"


def test_fsck_recover_selector_skips_init_under_tracker_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``fsck-recover`` self-resolves an injected tracker, so it must NOT auto-init then."""
    monkeypatch.setattr("rebar._commands.fsck_recover.fsck_recover_cli", lambda argv: 0)

    calls = _init_spy(monkeypatch)
    monkeypatch.setattr("rebar.config.tracker_dir_override", lambda: "/injected/tracker")
    _execute.execute("fsck-recover", [])
    assert calls == [], "an injected tracker must suppress the auto-init"

    calls2 = _init_spy(monkeypatch)
    monkeypatch.setattr("rebar.config.tracker_dir_override", lambda: None)
    _execute.execute("fsck-recover", [])
    assert calls2 == [False], "with no injected tracker fsck-recover inits full"


def test_export_and_import_split_their_init_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("rebar._io._cli.export_cli", lambda argv: 0)
    monkeypatch.setattr("rebar._io._cli.import_cli", lambda argv: 0)

    calls = _init_spy(monkeypatch)
    _execute.execute("export", [])
    assert calls == [True], "export reads → init_only"

    calls2 = _init_spy(monkeypatch)
    _execute.execute("import", [])
    assert calls2 == [False], "import composes writes → full"


# --- the router adds NO second parse: the selected S2 factory parses once ------


def test_selected_factory_parses_exactly_once_and_unrelated_stay_unimported() -> None:
    """A parser spy proves the router does not re-parse and imports no sibling factory.

    Run in a fresh child so the sys.modules census is uncontaminated. ``show`` is
    dispatched; its S2 factory ``build_show`` must be called exactly once (the
    handler's single parse), while ``build_list`` — a sibling core factory — must
    never be imported, and neither must an unrelated handler module (transition).
    """
    code = r"""
import sys, types
import rebar._cli._parsers.core.reads as reads_p
calls = {"show": 0}
_orig_show = reads_p.build_show
def spy_show(*a, **k):
    calls["show"] += 1
    return _orig_show(*a, **k)
reads_p.build_show = spy_show
# Trip a hard error if a sibling factory is even touched.
def boom(*a, **k):
    raise AssertionError("build_list must not be imported/called for `show`")
reads_p.build_list = boom

import rebar
r = rebar.init_repo(repo_root=".")
tid = rebar.create_ticket("task", "spy", repo_root=".")

from rebar._cli import main
rc = main(["show", tid])
assert rc == 0, rc
assert calls["show"] == 1, f"build_show called {calls['show']}x (router double-parsed?)"
assert "rebar._commands.transition" not in sys.modules, "unrelated handler imported"
print("SPY_OK")
"""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        subprocess.run(["git", "init", "-q"], cwd=d, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=d, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=d, check=True)
        subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "root"], cwd=d, check=True)
        env = subprocess_env(REBAR_ROOT=d, REBAR_SYNC_PUSH="off")
        cp = subprocess.run(
            [sys.executable, "-c", code], cwd=d, env=env, capture_output=True, text=True
        )
    assert cp.returncode == 0, cp.stderr
    assert cp.stdout.strip().endswith("SPY_OK"), cp.stdout + cp.stderr


def test_dispatching_a_read_imports_no_unrelated_handler_or_optional_stack() -> None:
    """Import isolation: a core read pulls in the reads path, not bridge/llm/reconciler."""
    code = r"""
import sys, rebar
rebar.init_repo(repo_root=".")
tid = rebar.create_ticket("task", "iso", repo_root=".")
from rebar._cli import main
assert main(["show", tid]) == 0
forbidden = [
    "rebar._cli._bridge_commands",
    "rebar._commands.transition",
    "rebar_reconciler",
    "pydantic_ai",
]
leaked = [m for m in forbidden if m in sys.modules]
print("LEAK:" + ",".join(leaked) if leaked else "CLEAN")
"""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        subprocess.run(["git", "init", "-q"], cwd=d, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=d, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=d, check=True)
        subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "root"], cwd=d, check=True)
        env = subprocess_env(REBAR_ROOT=d, REBAR_SYNC_PUSH="off")
        cp = subprocess.run(
            [sys.executable, "-c", code], cwd=d, env=env, capture_output=True, text=True
        )
    assert cp.returncode == 0, cp.stderr
    assert cp.stdout.strip().splitlines()[-1] == "CLEAN", cp.stdout + cp.stderr


# --- exception propagation ----------------------------------------------------


def test_handler_exception_propagates_out_of_execute(monkeypatch: pytest.MonkeyPatch) -> None:
    """A handler exception is NOT swallowed by the adapter layer."""

    def boom(argv):
        raise RuntimeError("handler blew up")

    monkeypatch.setattr("rebar._commands.metrics.metrics_cli", boom)
    monkeypatch.setattr("rebar._cli.ensure_initialized", lambda **_k: None)
    with pytest.raises(RuntimeError, match="handler blew up"):
        _execute.execute("metrics", [])


def test_unknown_spelling_at_execute_is_a_loud_wiring_error() -> None:
    """A spelling with no route/handler surfaces loudly rather than mis-dispatching."""
    with pytest.raises((RuntimeError, KeyError)):
        _execute.execute("definitely-not-a-command", [])


# --- confirmation / global output extraction (router overlay) -----------------


def test_confirmable_verb_preserves_tokens_after_double_dash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The global --output/--quiet pre-extraction must not consume tokens after ``--``."""
    seen: list[list[str]] = []
    monkeypatch.setattr("rebar._commands.main", lambda argv: seen.append(argv) or 0)
    monkeypatch.setattr("rebar._cli.ensure_initialized", lambda **_k: None)
    monkeypatch.setattr("rebar._cli.ensure_store_mounted_best_effort", lambda: None)

    main(["comment", "tid", "--", "--output", "llm"])

    assert seen, "handler was never reached"
    argv = seen[0]
    # everything after `--` is preserved verbatim for the handler
    assert argv[-3:] == ["--", "--output", "llm"], argv


def test_malformed_global_output_flag_on_confirmable_verb_exits_two(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A bad global --output value fails at extraction (exit 2, stderr) before dispatch."""
    called = {"n": 0}
    monkeypatch.setattr(
        "rebar._commands.main", lambda argv: called.__setitem__("n", called["n"] + 1) or 0
    )
    monkeypatch.setattr("rebar._cli.ensure_initialized", lambda **_k: None)
    monkeypatch.setattr("rebar._cli.ensure_store_mounted_best_effort", lambda: None)

    rc = main(["comment", "tid", "--output", "not-a-format"])

    assert rc == 2
    assert "Error" in capsys.readouterr().err
    assert called["n"] == 0, "handler must not run when the global flag is malformed"


def test_legacy_output_verbs_reinject_the_extracted_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A legacy-output verb (create) still receives ``--output <fmt>`` for its own parse."""
    seen: list[list[str]] = []
    monkeypatch.setattr("rebar._commands.main", lambda argv: seen.append(argv) or 0)
    monkeypatch.setattr("rebar._cli.ensure_initialized", lambda **_k: None)
    monkeypatch.setattr("rebar._cli.ensure_store_mounted_best_effort", lambda: None)

    main(["create", "task", "T", "--output", "json"])

    assert seen and seen[0][:3] == ["create", "task", "T"]
    assert "--output" in seen[0] and "json" in seen[0]


# --- bridge vocabulary through the closed adapters ----------------------------


def test_bridge_status_prefixes_status_through_the_bridge_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hidden ``bridge-status`` dispatches ``bridge_cli(["status", *rest])``."""
    seen: list[list[str]] = []
    monkeypatch.setattr(
        "rebar._cli._bridge_commands.bridge_cli", lambda argv: seen.append(argv) or 0
    )
    monkeypatch.setattr("rebar._cli.ensure_initialized", lambda **_k: None)
    _execute.execute("bridge-status", ["--json"])
    assert seen == [["status", "--json"]]


def test_bridge_canonical_passes_the_remainder_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr(
        "rebar._cli._bridge_commands.bridge_cli", lambda argv: seen.append(argv) or 0
    )
    monkeypatch.setattr("rebar._cli.ensure_initialized", lambda **_k: None)
    _execute.execute("bridge", ["status", "--json"])
    assert seen == [["status", "--json"]]


def test_purge_bridge_remains_unknown() -> None:
    """The retired bridge verb is not a route and never dispatches."""
    assert _registry.route_for("purge-bridge") is None
    code = main(["purge-bridge"])
    assert code == 1


# --- rollback seam: derived compatibility exports still reproduce the router ---


def _codes(routes: list[_registry.Route]) -> set[str]:
    return {f.code for f in _registry.validate(routes)}


def _exec_route(name: str, **kw: object) -> _registry.Route:
    kw.setdefault("group", "reads_init_only")
    return _registry.Route(name=name, **kw)  # type: ignore[arg-type]


def test_validate_rejects_an_adapter_outside_the_closed_set() -> None:
    """A route naming an adapter kind not in ADAPTER_KINDS is a wiring fault."""
    assert "unknown_adapter" in _codes([_exec_route("x", adapter="teleport")])


def test_validate_rejects_an_init_policy_outside_the_closed_set() -> None:
    assert "unknown_init" in _codes([_exec_route("x", init="sometimes")])


def test_validate_rejects_a_handler_without_an_adapter() -> None:
    """A handler with no adapter cannot be invoked — the executor could not pick a shape."""
    assert "handler_without_adapter" in _codes([_exec_route("x", handler="m:f", adapter="")])


def test_validate_rejects_an_argv_prefix_on_a_non_argv_adapter() -> None:
    """``argv_prefix`` is only meaningful for the ``argv`` adapter."""
    assert "prefix_without_argv" in _codes(
        [_exec_route("x", adapter="dispatcher", argv_prefix=("status",))]
    )


def test_validate_accepts_wellformed_execution_metadata() -> None:
    """A live route with a valid adapter/init/handler raises no execution finding."""
    codes = _codes(
        [
            _exec_route(
                "x", adapter="argv", init="full", handler="rebar._commands.metrics:metrics_cli"
            )
        ]
    )
    for code in (
        "unknown_adapter",
        "unknown_init",
        "handler_without_adapter",
        "prefix_without_argv",
    ):
        assert code not in codes


def test_execution_findings_are_excluded_from_retired_routes() -> None:
    """A retired route is unrouted, so its execution metadata is never validated."""
    retired = _exec_route("zzz", retired=True, adapter="teleport", init="sometimes")
    assert not (_codes([retired]) & {"unknown_adapter", "unknown_init"})


def test_router_uses_the_registry_derived_sets_as_its_sole_authority() -> None:
    """RP-05 S6: the router's live policy sets ARE the registry-derived sets, and the
    migration-only duplicate literal frozensets were retired (single-authority contract)."""
    from rebar import _cli

    derived = _registry.derive_policy_sets()
    for name in ("_INTERCEPTS", "_NO_AUTO_MOUNT", "_LEGACY_OUTPUT", "_CONFIRM_SCOPE"):
        assert getattr(_cli, name) == derived[name], name
    for name in ("_READS_INIT_ONLY", "_WRITES_FULL", "_LIFECYCLE", "_BRIDGE"):
        assert not hasattr(_cli, name), f"duplicate policy literal {name} still shipped"
