"""The v2 worklist interpreter: branch / loop / map execution, iteration-keyed
idempotency markers, the mandatory max_iterations runaway guard, and exactly-once
replay across crash points (the deepest-risk acceptance criterion, mirroring
docs/experiments/workflow-remediation-pocs/engine_interpreter_poc.py against the REAL
interpreter).
"""

from __future__ import annotations

import pytest

from rebar.llm.workflow import executor as _ex

# ── A replay-capable recorder + an idempotent side-effect sink ─────────────────


class _Crash(Exception):
    """Simulates a process crash after a committed marker."""


class _ReplayRecorder(_ex.RunRecorder):
    """An in-memory stand-in for the event-log recorder: persists FINAL markers by
    frame_key (last-writer-wins, like the reducer), answers ``completed_step`` from
    them, and can crash after the Nth committed marker. A shared ``store``/``sink``
    survive across a crash+replay (the event log + external world do)."""

    def __init__(self, store=None, sink=None, crash_after=None):
        self.store = store if store is not None else {}
        self.sink = sink if sink is not None else {}
        self.events: list[dict] = []
        self.crash_after = crash_after
        self.commits = 0

    def run_started(self, record): ...
    def run_finished(self, record): ...

    def step_recorded(self, record):
        self.events.append(dict(record))
        if record.get("status") == "running":
            return  # progress only — never a done-marker
        fk = record.get("frame_key") or record.get("step_id")
        self.store[fk] = dict(record)  # commit the durable marker
        self.commits += 1
        if self.crash_after is not None and self.commits >= self.crash_after:
            self.crash_after = None
            raise _Crash(f"crash after {self.commits} commits")

    def completed_step(self, run_id, frame_key):
        rec = self.store.get(frame_key)
        return rec if rec and rec.get("status") == "succeeded" else None


def _sink_step(message_fn):
    """Build a scripted step that emits ONCE per frame_key into the shared sink
    (idempotent on the frame_key token, like a real side-effecting step) and returns
    a small output."""

    def step(ctx: _ex.StepContext) -> _ex.StepResult:
        token = ctx.frame_key
        msg = message_fn(ctx)
        # Idempotent: a re-applied effect (replay after a crash-between-effect-and-
        # marker) is a no-op — the marker-after-effect rule rests on this.
        sink = _CURRENT_SINK[0]
        if token not in sink:
            sink[token] = msg
        return _ex.StepResult(outputs={"msg": msg, "i": ctx.iteration})

    return step


_CURRENT_SINK: list[dict] = [{}]


def _run(doc, *, recorder, registry, inputs=None):
    return _ex.run_workflow(
        doc,
        inputs or {},
        recorder=recorder,
        scripted_registry=registry,
        agent_runner=_ex.FakeAgentRunner(),
    )


# ── Workflows ──────────────────────────────────────────────────────────────────


def _branch_wf(flag: bool) -> dict:
    return {
        "schema_version": "2",
        "name": "br",
        "inputs": {"flag": {"type": "boolean"}},
        "steps": [
            {"id": "start", "uses": "emit"},
            {
                "id": "gate",
                "needs": ["start"],
                "branch": {
                    "when": "${{ inputs.flag }}",
                    "then": [{"id": "yes", "uses": "emit"}],
                    "else": [{"id": "no", "uses": "emit"}],
                },
            },
        ],
    }


def _loop_until_wf(max_iter=10) -> dict:
    # "refine until the attempt's score reaches 3": the condition reads the PREVIOUS
    # iteration's recorded body output (the POC's hard case).
    return {
        "schema_version": "2",
        "name": "lp",
        "steps": [
            {"id": "start", "uses": "emit"},
            {
                "id": "L",
                "needs": ["start"],
                "loop": {
                    "max_iterations": max_iter,
                    "until": "${{ steps.attempt.outputs.done }}",
                    "var": "i",
                    "body": [{"id": "attempt", "uses": "score"}],
                },
            },
        ],
    }


def _map_wf() -> dict:
    return {
        "schema_version": "2",
        "name": "mp",
        "inputs": {"items": {"type": "array"}},
        "steps": [
            {"id": "start", "uses": "emit"},
            {
                "id": "M",
                "needs": ["start"],
                "map": {
                    "over": "${{ inputs.items }}",
                    "as": "item",
                    "body": [{"id": "proc", "uses": "proc"}],
                },
            },
        ],
    }


def _registry():
    return {
        "emit": _sink_step(lambda ctx: f"emit:{ctx.frame_key}"),
        "proc": _sink_step(lambda ctx: f"proc:{ctx.frame_key}"),
        # score grows with iteration; "done" once i>=2 (so it runs iterations 0,1,2).
        "score": lambda ctx: _ex.StepResult(
            outputs={"done": (ctx.iteration or 0) >= 2, "score": (ctx.iteration or 0) + 1}
        ),
    }


# ── Execution ──────────────────────────────────────────────────────────────────


def test_branch_runs_then_arm():
    _CURRENT_SINK[0] = {}
    rec = _ReplayRecorder(sink=_CURRENT_SINK[0])
    res = _run(_branch_wf(True), recorder=rec, registry=_registry(), inputs={"flag": True})
    assert res.status == "succeeded"
    assert "gate@then/yes" in rec.store
    assert "gate@else/no" not in rec.store
    assert res.outputs["gate"] == {"taken": "then"}


def test_branch_runs_else_arm():
    _CURRENT_SINK[0] = {}
    rec = _ReplayRecorder(sink=_CURRENT_SINK[0])
    _run(_branch_wf(False), recorder=rec, registry=_registry(), inputs={"flag": False})
    assert "gate@else/no" in rec.store
    assert "gate@then/yes" not in rec.store


def test_loop_iterates_until_condition_keyed_by_iteration():
    _CURRENT_SINK[0] = {}
    rec = _ReplayRecorder(sink=_CURRENT_SINK[0])
    res = _run(_loop_until_wf(), recorder=rec, registry=_registry())
    assert res.status == "succeeded"
    # until done (i>=2): iterations 0,1,2 run, then the i=3 check stops it.
    assert {"L#0/attempt", "L#1/attempt", "L#2/attempt"} <= set(rec.store)
    assert "L#3/attempt" not in rec.store
    assert res.outputs["L"] == {"iterations": 3}


def test_map_runs_body_per_element():
    _CURRENT_SINK[0] = {}
    rec = _ReplayRecorder(sink=_CURRENT_SINK[0])
    res = _run(_map_wf(), recorder=rec, registry=_registry(), inputs={"items": ["a", "b", "c"]})
    assert res.status == "succeeded"
    assert {"M#0/proc", "M#1/proc", "M#2/proc"} <= set(rec.store)
    assert res.outputs["M"] == {"count": 3}
    assert rec.sink["M#1/proc"] == "proc:M#1/proc"


def test_loop_max_iterations_runaway_is_a_hard_error():
    _CURRENT_SINK[0] = {}
    rec = _ReplayRecorder(sink=_CURRENT_SINK[0])
    # A loop that never satisfies `until` within the cap -> runaway error, not a
    # silent stop.
    wf = {
        "schema_version": "2",
        "name": "runaway",
        "steps": [
            {
                "id": "L",
                "loop": {
                    "max_iterations": 3,
                    "until": "${{ steps.never.outputs.done }}",
                    "body": [{"id": "never", "uses": "noop"}],
                },
            }
        ],
    }
    reg = {"noop": lambda ctx: _ex.StepResult(outputs={"done": False})}
    res = _run(wf, recorder=rec, registry=reg)
    assert res.status == "failed"
    assert "max_iterations" in (res.error or "")


def test_loop_condition_resolution_error_mid_loop_fails_loudly():
    # A genuine mid-loop condition failure (the referenced body output never exists)
    # must FAIL the run with the cause, not silently end the loop. At i=0 the "no prior
    # output yet" case is the do-while exception (it runs the first iteration); at i=1
    # the prior iteration ran but produced no `ghost`, so resolution fails -> error.
    _CURRENT_SINK[0] = {}
    rec = _ReplayRecorder(sink=_CURRENT_SINK[0])
    wf = {
        "schema_version": "2",
        "name": "badcond",
        "steps": [
            {
                "id": "L",
                "loop": {
                    "max_iterations": 5,
                    "until": "${{ steps.attempt.outputs.ghost }}",
                    "body": [{"id": "attempt", "uses": "score"}],
                },
            }
        ],
    }
    res = _run(wf, recorder=rec, registry=_registry())
    assert res.status == "failed"
    assert "condition failed" in (res.error or "")
    # The do-while first iteration DID run before the failure surfaced.
    assert "L#0/attempt" in rec.store


def test_loop_without_condition_runs_exactly_max_iterations():
    _CURRENT_SINK[0] = {}
    rec = _ReplayRecorder(sink=_CURRENT_SINK[0])
    wf = {
        "schema_version": "2",
        "name": "fixed",
        "steps": [
            {"id": "L", "loop": {"max_iterations": 4, "body": [{"id": "tick", "uses": "emit"}]}}
        ],
    }
    res = _run(wf, recorder=rec, registry=_registry())
    assert res.status == "succeeded"
    assert res.outputs["L"] == {"iterations": 4}
    assert {"L#0/tick", "L#1/tick", "L#2/tick", "L#3/tick"} <= set(rec.store)


# ── Replay (the deepest-risk acceptance criterion) ─────────────────────────────


def _uninterrupted(wf, registry, inputs=None):
    sink: dict = {}
    _CURRENT_SINK[0] = sink
    rec = _ReplayRecorder(sink=sink)
    res = _run(wf, recorder=rec, registry=registry, inputs=inputs)
    return res, dict(sink), dict(rec.store)


def test_replay_is_exactly_once_across_every_crash_point():
    # Mirror the engine POC against the real interpreter: for EACH crash point, crash
    # after k committed markers, then replay to completion sharing ONE store+sink, and
    # assert the side-effect stream is identical to an uninterrupted run (no dupes,
    # none missing) and the final outputs match.
    wf, registry = _loop_until_wf(), _registry()
    base_res, base_sink, base_store = _uninterrupted(wf, registry)
    total = len({k for k, v in base_store.items() if v.get("status") != "running"})

    for k in range(1, total + 1):
        store: dict = {}
        sink: dict = {}
        _CURRENT_SINK[0] = sink
        # First process: crash after k commits.
        rec1 = _ReplayRecorder(store=store, sink=sink, crash_after=k)
        try:
            _run(wf, recorder=rec1, registry=registry)
        except _Crash:
            pass
        # Replay: same store + sink, no crash, run to completion.
        _CURRENT_SINK[0] = sink
        rec2 = _ReplayRecorder(store=store, sink=sink)
        res2 = _run(wf, recorder=rec2, registry=registry)
        assert res2.status == "succeeded", f"crash@{k}: {res2.error}"
        assert sink == base_sink, f"crash@{k}: side effects diverged"
        assert res2.outputs == base_res.outputs, f"crash@{k}: final outputs diverged"


def test_replay_resumes_a_loop_at_the_right_iteration():
    # Crash mid-loop, replay, and confirm completed iterations are NOT re-run (their
    # markers already committed) while the loop still completes to the same state.
    wf, registry = _loop_until_wf(), _registry()
    store: dict = {}
    sink: dict = {}
    _CURRENT_SINK[0] = sink
    rec1 = _ReplayRecorder(store=store, sink=sink, crash_after=2)  # after start + L#0/attempt
    with pytest.raises(_Crash):
        _run(wf, recorder=rec1, registry=registry)
    committed_before = {k for k, v in store.items() if v.get("status") == "succeeded"}
    # Replay records which frame_keys ACTUALLY executed this process.
    executed: list[str] = []
    base_score = registry["score"]

    def tracking_score(ctx):
        executed.append(ctx.frame_key)
        return base_score(ctx)

    registry = {**registry, "score": tracking_score}
    _CURRENT_SINK[0] = sink
    rec2 = _ReplayRecorder(store=store, sink=sink)
    res2 = _run(wf, recorder=rec2, registry=registry)
    assert res2.status == "succeeded"
    # An already-committed loop iteration must not re-execute on replay.
    for fk in committed_before:
        if fk.startswith("L#"):
            assert fk not in executed, f"{fk} re-ran on replay"
    assert res2.outputs["L"] == {"iterations": 3}


# ── Error paths, boundaries, and the if: skip-guard (behavioral coverage) ──────


def test_map_over_not_a_list_fails_the_run():
    _CURRENT_SINK[0] = {}
    rec = _ReplayRecorder(sink=_CURRENT_SINK[0])
    res = _run(_map_wf(), recorder=rec, registry=_registry(), inputs={"items": 42})
    assert res.status == "failed"
    assert "did not yield a list" in (res.error or "")


def test_map_over_empty_runs_zero_iterations():
    _CURRENT_SINK[0] = {}
    rec = _ReplayRecorder(sink=_CURRENT_SINK[0])
    res = _run(_map_wf(), recorder=rec, registry=_registry(), inputs={"items": []})
    assert res.status == "succeeded"
    assert res.outputs["M"] == {"count": 0}
    assert not any(k.startswith("M#") for k in rec.store)  # no body iterations


def test_unknown_scripted_step_fails_the_run():
    _CURRENT_SINK[0] = {}
    rec = _ReplayRecorder(sink=_CURRENT_SINK[0])
    wf = {"schema_version": "2", "name": "u", "steps": [{"id": "a", "uses": "nonexistent"}]}
    res = _run(wf, recorder=rec, registry=_registry())
    assert res.status == "failed"
    assert "unknown scripted step" in (res.error or "") and "nonexistent" in (res.error or "")


def test_declared_input_not_supplied_at_runtime_fails_the_run():
    # The input IS declared (so the linter passes), but is not supplied for this run —
    # the runtime resolver raises and the step fails (vs an UNDECLARED input, which the
    # linter rejects before execution).
    _CURRENT_SINK[0] = {}
    rec = _ReplayRecorder(sink=_CURRENT_SINK[0])
    wf = {
        "schema_version": "2",
        "name": "mi",
        "inputs": {"needed": {"type": "string"}},
        "steps": [{"id": "a", "uses": "emit", "with": {"x": "${{ inputs.needed }}"}}],
    }
    res = _run(wf, recorder=rec, registry=_registry(), inputs={})  # 'needed' not passed
    assert res.status == "failed"
    assert "needed" in (res.error or "") and "not set" in (res.error or "")


def test_if_guard_skips_step_without_running_its_effect():
    _CURRENT_SINK[0] = {}
    sink = _CURRENT_SINK[0]
    rec = _ReplayRecorder(sink=sink)
    wf = {
        "schema_version": "2",
        "name": "guard",
        "inputs": {"flag": {"type": "boolean"}},
        "steps": [
            {"id": "start", "uses": "emit"},
            {"id": "maybe", "needs": ["start"], "uses": "emit", "if": "${{ inputs.flag }}"},
            {"id": "after", "needs": ["maybe"], "uses": "emit"},
        ],
    }
    res = _run(wf, recorder=rec, registry=_registry(), inputs={"flag": False})
    assert res.status == "succeeded"
    # The guarded step is recorded SKIPPED, its effect never emitted; downstream still runs.
    assert rec.store["maybe"]["status"] == "skipped"
    assert "maybe" not in sink  # the side effect did not happen
    assert "start" in sink and "after" in sink


def test_if_guard_runs_step_when_truthy():
    _CURRENT_SINK[0] = {}
    sink = _CURRENT_SINK[0]
    rec = _ReplayRecorder(sink=sink)
    wf = {
        "schema_version": "2",
        "name": "guard",
        "inputs": {"flag": {"type": "boolean"}},
        "steps": [{"id": "a", "uses": "emit", "if": "${{ inputs.flag }}"}],
    }
    res = _run(wf, recorder=rec, registry=_registry(), inputs={"flag": True})
    assert res.status == "succeeded" and "a" in sink


def test_run_result_exposes_terminal_step_and_output():
    _CURRENT_SINK[0] = {}
    rec = _ReplayRecorder(sink=_CURRENT_SINK[0])
    res = _run(_branch_wf(True), recorder=rec, registry=_registry(), inputs={"flag": True})
    assert res.terminal_step == "gate"
    assert res.terminal_output == {"taken": "then"}


def test_while_is_a_pre_check_vs_until_do_while():
    # `while` is a PRE-check and `until` a POST-check (do-while). With a condition that
    # references the body's OWN output (unavailable at i=0): `until` runs the first
    # iteration then re-checks; `while` runs ZERO iterations (the i=0 pre-check has no
    # prior output -> falsy). This is the load-bearing semantic fork between the two.
    def body_fn(out):
        return lambda ctx: _ex.StepResult(outputs=out)

    def loop_wf(cond_key, cond_expr):
        return {
            "schema_version": "2",
            "name": "c",
            "steps": [
                {
                    "id": "L",
                    "loop": {
                        "max_iterations": 5,
                        cond_key: cond_expr,
                        "var": "i",
                        "body": [{"id": "a", "uses": "a"}],
                    },
                }
            ],
        }

    # until: body sets done=True on iter 0 -> runs iter 0 (do-while), i=1 check stops it.
    _CURRENT_SINK[0] = {}
    rec_u = _ReplayRecorder(sink=_CURRENT_SINK[0])
    res_u = _run(
        loop_wf("until", "${{ steps.a.outputs.done }}"),
        recorder=rec_u,
        registry={"a": body_fn({"done": True})},
    )
    assert res_u.outputs["L"] == {"iterations": 1}

    # while: pre-check at i=0 has no prior body output -> falsy -> zero iterations.
    _CURRENT_SINK[0] = {}
    rec_w = _ReplayRecorder(sink=_CURRENT_SINK[0])
    res_w = _run(
        loop_wf("while", "${{ steps.a.outputs.go }}"),
        recorder=rec_w,
        registry={"a": body_fn({"go": True})},
    )
    assert res_w.outputs["L"] == {"iterations": 0}
    assert not any(k.startswith("L#") for k in rec_w.store)


def test_map_index_var_resolves_at_runtime():
    _CURRENT_SINK[0] = {}
    rec = _ReplayRecorder(sink=_CURRENT_SINK[0])
    indices: list = []

    def proc(ctx):
        indices.append(ctx.inputs.get("j"))
        return _ex.StepResult(outputs={})

    wf = {
        "schema_version": "2",
        "name": "mi",
        "inputs": {"xs": {"type": "array"}},
        "steps": [
            {
                "id": "M",
                "map": {
                    "over": "${{ inputs.xs }}",
                    "as": "item",
                    "index_var": "ix",
                    "body": [{"id": "proc", "uses": "proc", "with": {"j": "${{ map.ix }}"}}],
                },
            }
        ],
    }
    res = _run(wf, recorder=rec, registry={"proc": proc}, inputs={"xs": ["a", "b", "c"]})
    assert res.status == "succeeded"
    assert indices == [0, 1, 2]
