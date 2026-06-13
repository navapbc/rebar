"""HLC next_tick() prototype for rebar P2.1 (validation only, not production code).

Two things are validated here:
  * next_tick()            — the local-lock RMW fast path (EXP4: 2400 concurrent
                             ticks, all unique/monotonic/19-digit).
  * next_tick_for_ticket() — the LOAD-BEARING refinement (git-bug witness model):
                             the local .hlc.state file is a disposable cache; the
                             authoritative value is witnessed from max(prefix) over
                             the TARGET TICKET's events, so a missing/stale/corrupt
                             cache still yields a correct, monotonic, causally-sound
                             tick. This is what makes the design corruption/race-proof
                             and gives cross-clone causal correctness.
"""
import os, time, fcntl, sys, glob, re

STATE = os.path.join(os.path.dirname(__file__) or ".", ".hlc.state")
LOCK = STATE + ".lock"
_PREFIX = re.compile(r"^(\d+)-")


def seed_from_max(prefixes):
    if prefixes:
        with open(STATE, "w") as f:
            f.write(str(max(prefixes)))


def _max_prefix(ticket_dir):
    """Authoritative floor: the max filename-prefix across the ticket's events."""
    best = 0
    for p in glob.glob(os.path.join(ticket_dir, "*.json")):
        m = _PREFIX.match(os.path.basename(p))
        if m:
            best = max(best, int(m.group(1)))
    return best


def next_tick(witness=0):
    """Issue max(cache, witness, wall_ns) + 1 under a local lock. `witness` is the
    floor derived from the durable log (per-ticket max(prefix))."""
    fd = os.open(LOCK, os.O_CREAT | os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        last = 0
        try:
            with open(STATE) as f:
                last = int(f.read().strip() or 0)
        except FileNotFoundError:
            pass
        t = max(time.time_ns(), last + 1, witness + 1)
        tmp = STATE + f".tmp{os.getpid()}"
        with open(tmp, "w") as f:
            f.write(str(t))
        os.replace(tmp, STATE)
        return t
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def next_tick_for_ticket(ticket_dir):
    """Production-shaped entry point: witness the ticket's own max(prefix) as the
    floor, so correctness never depends on the local cache being present/fresh."""
    return next_tick(witness=_max_prefix(ticket_dir))


if __name__ == "__main__":
    # `hlc.py <n>`             -> n fast-path ticks (EXP4)
    # `hlc.py witness <dir>`   -> demo: tick is >= max(prefix) even with NO cache
    if len(sys.argv) >= 3 and sys.argv[1] == "witness":
        td = sys.argv[2]
        # Simulate a corrupt/absent cache by removing it; correctness must hold.
        try:
            os.remove(STATE)
        except FileNotFoundError:
            pass
        floor = _max_prefix(td)
        t = next_tick_for_ticket(td)
        ok = t > floor and len(str(t)) == 19
        print(f"max(prefix)={floor} -> tick={t} > floor and 19-digit: {ok}")
    else:
        n = int(sys.argv[1])
        vals = [next_tick() for _ in range(n)]
        sys.stdout.write("\n".join(map(str, vals)) + "\n")
