"""A cursor-aware `journalctl` stub, shared by every observability script test (bug 1205).

`observability.sh` used to derive its marker deltas by re-counting the WHOLE retained journal
every run — thirteen times per run across its counters — which took Gerrit off the air for 41
minutes on 2026-09-04. It now reads only the entries after a persisted journald cursor.

The stubs these tests used before were argument-BLIND (`cat "$JOURNAL_FILE"`), returning the
entire journal however they were asked for it. That is precisely why nothing here noticed the
scans were unbounded: a stub that cannot tell a bounded request from an unbounded one cannot
fail when the caller stops bounding. This emulator honours the arguments instead.

Journal model: `$JOURNAL_FILE` holds the entries currently RETAINED, one per line, and
`$JOURNAL_BASE_FILE` (optional, default 0) holds how many entries rotation has already
discarded. An entry's cursor is therefore its position in the journal's whole history, which is
what makes rotation expressible: a cursor below the base names an entry journald no longer
holds, and the seek fails exactly as the real one does.
"""

from __future__ import annotations

JOURNALCTL_EMULATOR = """
    after=""
    since=""
    tail_only=0
    show_cursor=0
    while [ $# -gt 0 ]; do
      case "$1" in
        --after-cursor) after="$2"; shift 2 ;;
        --after-cursor=*) after="${1#*=}"; shift ;;
        --since) since="$2"; shift 2 ;;
        --since=*) since="${1#*=}"; shift ;;
        -n) tail_only="$2"; shift 2 ;;
        --lines) tail_only="$2"; shift 2 ;;
        --lines=*) tail_only="${1#*=}"; shift ;;
        --show-cursor) show_cursor=1; shift ;;
        *) shift ;;
      esac
    done

    base=0
    if [ -n "${JOURNAL_BASE_FILE:-}" ] && [ -f "$JOURNAL_BASE_FILE" ]; then
      base=$(tr -dc '0-9' < "$JOURNAL_BASE_FILE")
      base=${base:-0}
    fi
    total=0
    if [ -f "$JOURNAL_FILE" ]; then
      total=$(wc -l < "$JOURNAL_FILE" | tr -d ' ')
    fi
    last=$((base + total))

    start=1
    if [ -n "$after" ]; then
      if [ "$after" -lt "$base" ]; then
        echo "Failed to seek to cursor: Invalid argument" >&2
        exit 1
      fi
      start=$((after - base + 1))
    elif [ "$tail_only" != 0 ]; then
      start=$((total - tail_only + 1))
      [ "$start" -lt 1 ] && start=1
    fi

    emitted=0
    if [ "$total" -gt 0 ] && [ "$start" -le "$total" ]; then
      sed -n "${start},${total}p" "$JOURNAL_FILE"
      emitted=$((total - start + 1))
    fi
    if [ -n "${EMITTED_FILE:-}" ]; then
      printf '%s\\n' "$emitted" >> "$EMITTED_FILE"
    fi
    # Real journalctl prints the cursor of the last entry it SHOWED, so an empty result carries
    # no cursor line.
    if [ "$show_cursor" -eq 1 ] && [ "$emitted" -gt 0 ]; then
      printf -- '-- cursor: %s\\n' "$last"
    fi
    exit 0
"""

# A faithful `timeout`, provided explicitly rather than inherited: coreutils `timeout` does not
# exist on macOS, so a test that relied on the host's would exercise the wall-clock bound on
# Linux CI and silently skip it everywhere else. It signals the process GROUP, as GNU timeout
# does, because bash handles SIGALRM itself and because a grandchild holding stdout open keeps
# a command substitution waiting long after its parent is dead.
TIMEOUT_STUB = """
    exec perl -e '
      use POSIX ":sys_wait_h";
      my $t = shift;
      my $pid = fork();
      if ($pid == 0) { POSIX::setpgid(0, 0); exec @ARGV; exit 127; }
      POSIX::setpgid($pid, $pid);
      my $deadline = time() + $t;
      while (1) {
        if (waitpid($pid, WNOHANG) == $pid) { exit($? >> 8); }
        if (time() >= $deadline) {
          kill "TERM", -$pid; kill "KILL", -$pid; waitpid($pid, 0); exit 124;
        }
        select(undef, undef, undef, 0.1);
      }
    ' "$@"
"""
