# Preventing and recovering orphaned helper processes

This procedure covers background helpers started for development and investigation work. It explains how to prevent a helper from surviving its task, how to detect an orphan without changing host state, and how an operator can reclaim a confirmed orphan.

## Prevention contract

Every unbounded helper needs both a wall-clock bound at spawn and process-group cleanup in the spawning shell. A cleanup step that runs only at the end of a task is insufficient because an interrupted or killed harness never reaches that step.

```sh
# Stop the helper even if the shell never returns normally.
timeout 120 python -c 'while True: pass' &
# Reap every helper in this process group when the shell exits.
trap 'kill 0' EXIT INT TERM
```

The `timeout` ends the helper after the declared interval. The trap signals the process group on normal exit, interruption, or error. A subagent must reap every process it starts before returning. No process started for task work may reach PPID 1.

Prefer a bounded workload with a fixed end condition. Never start an unbounded busy loop such as `while True: pass`, `yes > /dev/null`, or `while :; do :; done` without a wall-clock bound. Lowering process priority with `nice` does not provide a bound.

## Bounded gate operations

Do not apply `timeout` to `review-plan`, `verify-completion`, a completion-verifier-gated close, or another LLM gate operation that terminates with a verdict. Truncating a gate wastes the billable run and can make an imposed timeout appear to be a gate failure. Use the asynchronous MCP starter and status tools when a gate can outlast a client request.

## Incident evidence

On 2026-08-22, three agent sessions started CPU load generators while reproducing a timing failure. Thirty-eight helpers were reparented to PID 1 and ran for four days at about 1341 percent combined CPU on a six-performance-core host. The load average reached 58, applications stopped launching, and timing-sensitive measurements taken during that period became unreliable.

The helpers had been started with `nice`, which reduced the visibility of the degradation but did not end the processes. Reclaiming the 38 helpers reduced the load average from 58 to 10.

## Read-only detection

The detector reports processes whose parent is PID 1 and whose accumulated CPU time exceeds the threshold. It never signals a process and is not part of `make lint` because it evaluates current host state rather than repository content.

```sh
python scripts/check_orphaned_load.py
python scripts/check_orphaned_load.py --min-cpu-seconds 600
python scripts/check_orphaned_load.py --include-system
```

The default threshold is 3600 CPU-seconds. OS vendor and endpoint-management executables are suppressed by default, and the detector reports the suppressed count. Use `--include-system` when the investigation requires the complete PPID 1 population.

PPID 1 is a signal, not proof of a defect. `launchd` and `init` parent system daemons to PID 1 by design. One unfiltered run on the affected host reported 33 processes, including 29 system daemons. The four remaining entries included an orphaned agent job. Inspect command lines and confirm that the associated work has ended before treating a process as reclaimable.

## Operator recovery

Confirm that the investigation which started the helper has ended. Search by the distinctive command line and inspect every match before sending a signal.

```sh
pgrep -fl 'while True: pass'
```

After confirming the complete match set belongs to finished work, terminate that exact set.

```sh
pkill -f 'while True: pass'
```

Never use a broad or unresolved pattern for reclamation. Repeat the read-only detector after recovery to confirm that the intended processes are gone and that unrelated processes remain.
