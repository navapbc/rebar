"""
ENGINE DE-RISK POC — thin synchronous declarative interpreter with run-state on an
EXTERNAL event log, proving resume/replay correctness.

WHY THIS POC: the workflow-remediation brainstorm converged on BUILDING a thin
interpreter (not adopting DBOS/Restate/SpiffWorkflow) because every off-the-shelf
durable-execution engine OWNS its own persistence and cannot defer run-state to
rebar's git-backed event log. The pressure-test round flagged the *deepest
unvalidated risk* of that decision: can we get the replay determinism those engines
get from owned transactional stores, when our run-state instead lives in an external
append-only event log? This POC answers that, empirically, across the three
control-flow constructs the brainstorm requires: conditionals (route), loops
(bounded by max_iterations), and dynamic fan-out (map).

═══ FINDING FROM THE FIRST DRAFT (kept here because it IS the de-risk result) ═══
The first version FAILED: the loop counter lived as a MUTABLE SHARED context value
that a loop-body step bumped as a side effect. On replay, a completed step is skipped
and its recorded OUTPUT is replayed — but that out-of-band mutation was NOT, so the
loop counter reset and re-ran iterations, duplicating side effects (`iter-0` twice).

THE DESIGN RULE THIS PROVES (carry into the real engine): in an event-sourced replay
model, ALL control-flow state (loop counters, accumulators, branch inputs) must be
reconstructable from recorded events. A step may contribute ONLY its recorded output
(stored under its full frame path); it must not mutate shared state out of band. Loop
position is then derived from recorded outputs, so replay makes identical decisions.
This is the discipline that lets an external event log match DBOS/Restate's owned
transactional store. (rebar's real executor already keys outputs per step; the spike
noted loops need iteration-keyed markers — this POC shows concretely WHY.)

No third-party dependencies.  Run:  python3 engine_interpreter_poc.py
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ─────────────────────────────────────────────────────────────────────────────
# Event log (stand-in for rebar's WORKFLOW_STEP events on the ticket event log).
# Append-only, keyed by FULL FRAME PATH so a step inside a loop/map gets a distinct
# marker per iteration. The log is the SOLE source of truth for "what happened".
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class StepEvent:
    frame_key: str           # e.g. "L#2/attempt"  (loop L, iteration 2, step attempt)
    status: str              # "succeeded"
    output: object           # recorded output — replayed into context, never recomputed
    nondet: dict             # captured nondeterminism (clock/uuid/seed) — replayed, not re-rolled


@dataclass
class EventLog:
    events: list[StepEvent] = field(default_factory=list)

    def marker(self, frame_key: str) -> StepEvent | None:
        for ev in reversed(self.events):          # last-writer-wins, like rebar's reducer slot
            if ev.frame_key == frame_key:
                return ev
        return None

    def append(self, ev: StepEvent) -> None:
        self.events.append(ev)


class Crash(Exception):
    """Simulates a process crash after N committed events."""


# ─────────────────────────────────────────────────────────────────────────────
# External world: a side-effect sink with its OWN idempotency guard (belt &
# suspenders). Real side effects (e.g. posting a ticket comment) embed the frame
# key as a token so even an at-least-once re-application is a no-op.
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Sink:
    messages: list[str] = field(default_factory=list)
    _seen: set[str] = field(default_factory=set)

    def emit(self, token: str, message: str) -> None:
        if token in self._seen:
            return
        self._seen.add(token)
        self.messages.append(message)


# ─────────────────────────────────────────────────────────────────────────────
# Lean declarative IR (the tailored YAML/JSON shape the brainstorm chose):
#   {"type":"step",  "id", "fn"}
#   {"type":"route", "when": pred(ctx), "then":[...], "else":[...]}
#   {"type":"loop",  "id", "var", "max_iterations", "cont": pred(i, ctx), "body":[...]}
#   {"type":"map",   "id", "over": ctx-key, "as": var, "body":[...]}
#
# CONTRACT: a step fn(ctx, frame_key, nondet, sink) -> output. It may read ctx and
# emit to the sink (idempotently, with frame_key as token); its ONLY contribution to
# state is its return value, recorded under frame_key. No out-of-band ctx mutation.
# ─────────────────────────────────────────────────────────────────────────────


class Interpreter:
    """Synchronous, single-threaded, deterministic. No asyncio/threading/retries
    (keeps rebar's Burr tripwire intact)."""

    def __init__(self, *, steps, log: EventLog, sink: Sink, nondet_source,
                 crash_after: int | None = None):
        self.steps = steps
        self.log = log
        self.sink = sink
        self.nondet_source = nondet_source
        self.crash_after = crash_after
        self.committed_this_run = 0
        self.executed: list[str] = []          # frame_keys whose BODY actually ran THIS process

    def _run_step(self, node, ctx, frame):
        key = f"{frame}{node['id']}"
        marker = self.log.marker(key)
        if marker is not None and marker.status == "succeeded":
            ctx[key] = marker.output            # replay recorded output; DO NOT re-run or re-emit
            return
        nondet = self.nondet_source(key)
        output = self.steps[node["fn"]](ctx, key, nondet, self.sink)
        self.executed.append(key)
        ctx[key] = output
        self.log.append(StepEvent(key, "succeeded", output, nondet))
        self.committed_this_run += 1
        if self.crash_after is not None and self.committed_this_run >= self.crash_after:
            raise Crash(f"crash after {self.committed_this_run} committed events")

    def execute(self, nodes, ctx, frame=""):
        for node in nodes:
            t = node["type"]
            if t == "step":
                self._run_step(node, ctx, frame)
            elif t == "route":
                self.execute(node["then"] if node["when"](ctx) else node.get("else", []),
                             ctx, frame)
            elif t == "loop":
                i = 0
                last = None
                while node["cont"](i, ctx):
                    if i >= node["max_iterations"]:
                        raise RuntimeError(f"loop {node['id']} exceeded max_iterations")
                    child = dict(ctx)
                    child[node["var"]] = i
                    self.execute(node["body"], child, f"{frame}{node['id']}#{i}/")
                    ctx.update(child)            # promote recorded outputs (keyed by frame) upward
                    last = child
                    i += 1
                # loop summary = derived purely from recorded outputs -> stable on replay
                ctx[f"{frame}{node['id']}"] = (i, last)
            elif t == "map":
                for idx, item in enumerate(ctx[node["over"]]):
                    child = dict(ctx)
                    child[node["as"]] = item
                    self.execute(node["body"], child, f"{frame}{node['id']}#{idx}/")
                    ctx.update(child)
            else:
                raise ValueError(f"unknown node type {t!r}")
        return ctx


# ─────────────────────────────────────────────────────────────────────────────
# Workflow exercising all three constructs WITH side effects inside the loop and map
# (the hard case). Loop continuation depends on a RECORDED OUTPUT (the previous
# iteration's score), never on a mutated shared variable.
# ─────────────────────────────────────────────────────────────────────────────


def build_workflow():
    return [
        {"type": "step", "id": "start", "fn": "emit_start"},
        # "refine until score >= 3": cont reads the previous iteration's recorded attempt output.
        {"type": "loop", "id": "L", "var": "i", "max_iterations": 10,
         "cont": (lambda i, c: i == 0 or c.get(f"L#{i-1}/attempt", 0) < 3),
         "body": [{"type": "step", "id": "attempt", "fn": "attempt"}]},
        {"type": "route", "id": "R",
         "when": (lambda c: c.get("L", (0, None))[1] is not None
                  and c[f"L#{c['L'][0]-1}/attempt"] >= 3),
         "then": [{"type": "step", "id": "ok", "fn": "emit_ok"}],
         "else": [{"type": "step", "id": "bad", "fn": "emit_bad"}]},
        {"type": "map", "id": "M", "over": "items", "as": "item",
         "body": [{"type": "step", "id": "emit_item", "fn": "emit_item"}]},
        {"type": "step", "id": "done", "fn": "emit_done"},
    ]


def make_steps():
    def emit_start(ctx, key, nondet, sink):
        sink.emit(key, "start"); return "started"

    def attempt(ctx, key, nondet, sink):          # score grows deterministically with iteration
        i = ctx["i"]; sink.emit(key, f"iter-{i}"); return i + 1

    def emit_ok(ctx, key, nondet, sink):
        sink.emit(key, "route-ok"); return "ok"

    def emit_bad(ctx, key, nondet, sink):
        sink.emit(key, "route-bad"); return "bad"

    def emit_item(ctx, key, nondet, sink):
        sink.emit(key, f"item-{ctx['item']}"); return ctx["item"]

    def emit_done(ctx, key, nondet, sink):
        sink.emit(key, "done"); return "done"

    return {f.__name__: f for f in (emit_start, attempt, emit_ok, emit_bad, emit_item, emit_done)}


def deterministic_nondet(key):
    return {"seed": hash(key) & 0xffff}


def initial_ctx():
    return {"items": ["a", "b"]}


# ─────────────────────────────────────────────────────────────────────────────
# Experiment: uninterrupted run vs crash-then-replay at EVERY commit point.
# ─────────────────────────────────────────────────────────────────────────────


def run_uninterrupted():
    log, sink = EventLog(), Sink()
    interp = Interpreter(steps=make_steps(), log=log, sink=sink,
                         nondet_source=deterministic_nondet)
    ctx = interp.execute(build_workflow(), initial_ctx())
    return ctx, log, sink, interp.executed


def run_with_crash_and_replay(crash_after):
    log, sink = EventLog(), Sink()                 # ONE shared external log + sink across the crash
    interp1 = Interpreter(steps=make_steps(), log=log, sink=sink,
                          nondet_source=deterministic_nondet, crash_after=crash_after)
    crashed = False
    try:
        interp1.execute(build_workflow(), initial_ctx())
    except Crash:
        crashed = True
    interp2 = Interpreter(steps=make_steps(), log=log, sink=sink,
                          nondet_source=deterministic_nondet)
    interp2.execute(build_workflow(), initial_ctx())
    return crashed, log, sink, interp1.executed, interp2.executed


def main():
    print("=" * 72)
    print("ENGINE POC: replay correctness over an external event log")
    print("=" * 72)

    _, base_log, base_sink, base_exec = run_uninterrupted()
    print(f"\n[uninterrupted] messages ({len(base_sink.messages)}): {base_sink.messages}")
    print(f"[uninterrupted] steps executed: {len(base_exec)}")

    failures = []
    for crash_after in range(1, len(base_exec) + 1):
        crashed, log, sink, exec1, exec2 = run_with_crash_and_replay(crash_after)
        same_msgs = sink.messages == base_sink.messages          # no dupes, none missing, same order
        all_exec = exec1 + exec2
        exactly_once = (len(all_exec) == len(set(all_exec))       # nothing ran twice across crash+replay
                        and set(all_exec) == set(base_exec))
        resumed_remainder = set(exec2).isdisjoint(set(exec1))     # replay ran only the un-done steps
        ok = same_msgs and exactly_once and resumed_remainder
        print(f"[crash@{crash_after:>2}] {'ok ' if ok else 'FAIL'} "
              f"ran_before={len(exec1):>2} ran_after={len(exec2):>2} "
              f"msgs_match={same_msgs} exactly_once={exactly_once} resume_only={resumed_remainder}")
        if not ok:
            failures.append((crash_after, sink.messages))

    print("\n" + "=" * 72)
    if failures:
        print(f"RESULT: FAIL — {len(failures)} crash point(s) produced wrong state")
        for ca, msgs in failures:
            print(f"  crash@{ca}: {msgs}")
        raise SystemExit(1)
    print(f"RESULT: PASS — replay is exactly-once and deterministic across all "
          f"{len(base_exec)} crash points.")
    print("Side effects never duplicated; completed steps never re-run; loop resumed at the")
    print("right iteration; final side-effect stream identical to an uninterrupted run.")
    print("=> An external event log CAN give DBOS/Restate-grade replay determinism, PROVIDED")
    print("   control-flow state is derived from recorded outputs (not mutated out of band).")
    print("   Engine-build de-risked; the state-discipline rule is the key design constraint.")


if __name__ == "__main__":
    main()
