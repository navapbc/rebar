# Preventing and recovering orphaned helper processes

This procedure covers background helpers started for development and investigation work. It explains how to prevent a helper from surviving its task, how to detect an orphan without changing host state, and how an operator can reclaim a confirmed orphan.

## Prevention contract

Every unbounded helper needs both a wall-clock bound at spawn and a reap step in the spawning shell that survives interruption. A cleanup step that runs only at the end of a task is insufficient because an interrupted or killed harness never reaches that step.

**The prescription itself lives in exactly one place: [AGENTS.md](../AGENTS.md) section "Bound background helpers at spawn".** Read the `bound()` cascade and the mandatory rules there and follow them verbatim. This document deliberately does not restate the snippet, because the copy it used to carry drifted away from the original and went on prescribing a pattern that no-ops on macOS long after the original was corrected (bug `6a9d-4792-7099-4a17`).

Three consequences of that guidance are worth stating here, because each one is what turned a bounded helper into one of the orphans catalogued below:

- **A bounder that is not installed does not bind.** macOS ships neither `timeout` nor `gtimeout`, so a bare `timeout 120 <cmd> &` exits 127 with the helper never started, and announces that only on a backgrounded job's stderr. The cascade in AGENTS.md selects an installed bounder and refuses to spawn when the host has none.
- **`trap 'kill 0'` does not reap what you think it reaps.** `kill 0` signals the caller's process group. A subshell shares its parent's group, so the trap kills the script that installed it, while a wrapper shell above the script is never in that group and survives untouched. That surviving wrapper is the process that leaks. Kill the pids you recorded at spawn instead.
- **`pgrep -f <pattern>` matches command-line text, not identity.** A wait-until-nothing-matches loop also matches a sibling agent's identical waiter, a `ps | grep` pipeline, and on some platforms the waiter's own wrapper, so it can wait forever or signal an unrelated process. Wait on the pid you recorded.

Prefer a bounded workload with a fixed end condition. Never start an unbounded busy loop such as `while True: pass`, `yes > /dev/null`, or `while :; do :; done` without a wall-clock bound. Lowering process priority with `nice` does not provide a bound.

## Bounded gate operations

Do not apply a wall-clock bound to `review-plan`, `verify-completion`, a completion-verifier-gated close, `make verify`, or another LLM gate operation that terminates with a verdict. Truncating a gate wastes the billable run and can make an imposed timeout appear to be a gate failure. Use the asynchronous MCP starter and status tools when a gate can outlast a client request.

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

Never use a broad or unresolved pattern for reclamation. `pgrep`/`pkill` match command-line text rather than identity, so the match set can include a sibling session's live helper, the `pgrep` pipeline itself, or the shell that is doing the searching. Confirm every pid with `ps -o pid=,ppid=,command= -p <pid>` before signalling it. Repeat the read-only detector after recovery to confirm that the intended processes are gone and that unrelated processes remain.
