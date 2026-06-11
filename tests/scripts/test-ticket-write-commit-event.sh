#!/usr/bin/env bash
# tests/scripts/test-ticket-write-commit-event.sh
# RED tests for bash-native write_commit_event (task f340-a070, story 29e5-0a74,
# epic 78fc-3858).
#
# Verifies that write_commit_event:
#   1. Spawns zero python3 processes (bash-native path).
#   2. Produces byte-identical JSON to Python's json.dumps(ensure_ascii=False,
#      separators=(',',':'), sort_keys=True) for the same inputs — including
#      unicode, backslash, and double-quote in the title field.
#   3. Handles two concurrent callers without corruption.
#   4. Does not silently corrupt output when the lock file is already held.
#   5. Returns exit code 0 on success.
#
# Also tests the canonical bash-native write path (no regression).
#
# Usage: bash tests/scripts/test-ticket-write-commit-event.sh

# NOTE: -e is intentionally omitted — test functions return non-zero by design.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
TICKET_SCRIPT="$REPO_ROOT/src/rebar/_engine/ticket"
TICKET_LIB="$REPO_ROOT/src/rebar/_engine/ticket-lib.sh"

source "$REPO_ROOT/tests/lib/assert.sh"
source "$REPO_ROOT/tests/lib/git-fixtures.sh"

echo "=== test-ticket-write-commit-event.sh ==="

# Track temp dirs / sentinel files for cleanup.
# git-fixtures.sh already sets _CLEANUP_DIRS and the EXIT trap.

# Resolve the real python3 so the PATH shim can delegate to it.
_REAL_PYTHON3="$(command -v python3 2>/dev/null || true)"
if [ -z "$_REAL_PYTHON3" ] || [ ! -x "$_REAL_PYTHON3" ]; then
    _REAL_PYTHON3="/usr/bin/python3"
fi

# ── Helper: create a PATH shim dir with a counting python3 wrapper ────────────
# The shim appends "CALLED" to <sentinel> and then execs the real python3 so
# any downstream logic still functions correctly.
_make_python3_shim() {
    local sentinel="$1"
    local shim_dir
    shim_dir=$(mktemp -d)
    _CLEANUP_DIRS+=("$shim_dir")
    cat > "$shim_dir/python3" <<EOF
#!/usr/bin/env bash
echo "CALLED" >> "$sentinel"
exec "$_REAL_PYTHON3" "\$@"
EOF
    chmod +x "$shim_dir/python3"
    echo "$shim_dir"
}

# ── Helper: create a fresh temp git repo with ticket system initialized ────────
_make_test_repo() {
    local tmp
    tmp=$(mktemp -d)
    _CLEANUP_DIRS+=("$tmp")
    clone_ticket_repo "$tmp/repo"
    echo "$tmp/repo"
}

# ── Helper: build a minimal valid event JSON temp file ────────────────────────
# Usage: _make_event_json <repo> <ticket_id> <title>
# Writes a CREATE event JSON to a temp file and prints its path.
_make_event_json() {
    local repo="$1"
    local ticket_id="$2"
    local title="$3"
    local tmp_event
    tmp_event=$(mktemp)
    _CLEANUP_FILES+=("$tmp_event") 2>/dev/null || true
    # Build a minimal CREATE event JSON with the supplied title.
    python3 - "$tmp_event" "$ticket_id" "$title" <<'PYEOF'
import json, sys, uuid, datetime

out_path = sys.argv[1]
ticket_id = sys.argv[2]
title = sys.argv[3]

event = {
    "event_type": "CREATE",
    "timestamp": datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%S%f") + "Z",
    "uuid": str(uuid.uuid4()).replace("-", "")[:12],
    "data": {
        "ticket_id": ticket_id,
        "title": title,
        "type": "task",
        "priority": 4,
        "status": "open",
        "tags": [],
    },
}
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(event, f, ensure_ascii=False)
PYEOF
    echo "$tmp_event"
}

# ── Helper: build a minimal event JSON file at a specified path ───────────────
# Usage: _make_event_json_to_file <dest_path> <ticket_id>
# Used by legacy tests that pre-allocate a destination path.
_make_event_json_to_file() {
    local dest="$1"
    local ticket_id="$2"
    local ts
    ts=$(python3 -c "import time; print(int(time.time()))")
    local uuid_val
    uuid_val=$(python3 -c "import uuid; print(uuid.uuid4())")
    python3 -c "
import json, sys
data = {
    'timestamp': $ts,
    'uuid': '$uuid_val',
    'event_type': 'CREATE',
    'env_id': '$uuid_val',
    'author': 'Test',
    'data': {
        'ticket_type': 'task',
        'title': 'Test ticket',
        'parent_id': None
    }
}
json.dump(data, sys.stdout)
" > "$dest"
}

# ── Helper: create a ticket directory and return the ticket_id ────────────────
_init_ticket_dir() {
    local repo="$1"
    local ticket_id="ti00-test"
    mkdir -p "$repo/.tickets-tracker/$ticket_id"
    echo "$ticket_id"
}

# ── Test 1: write_commit_event spawns zero python3 processes ──────────────────
test_write_commit_event_no_python3() {
    local repo ticket_id event_json sentinel shim_dir
    repo=$(_make_test_repo)
    ticket_id=$(cd "$repo" && _TICKET_TEST_NO_SYNC=1 bash "$TICKET_SCRIPT" create task "No-python3 test" 2>/dev/null | tail -1)
    if [ -z "$ticket_id" ]; then
        echo "  setup failed: ticket create returned empty ID"
        return 1
    fi

    event_json=$(_make_event_json "$repo" "$ticket_id" "No-python3 test")

    sentinel=$(mktemp)
    rm -f "$sentinel"  # sentinel is absent until python3 is called
    _CLEANUP_FILES+=("$sentinel") 2>/dev/null || true

    shim_dir=$(_make_python3_shim "$sentinel")

    # Source ticket-lib and call write_commit_event with shimmed PATH.
    local exit_code=0
    (
        cd "$repo"
        PATH="$shim_dir:$PATH" _TICKET_TEST_NO_SYNC=1 \
            bash -c "
                source '$TICKET_LIB'
                write_commit_event '$ticket_id' '$event_json'
            " 2>/dev/null
    ) || exit_code=$?

    if [ "$exit_code" -ne 0 ]; then
        echo "  write_commit_event exited $exit_code (expected 0)"
        return 1
    fi

    if [ -f "$sentinel" ]; then
        local call_count
        call_count=$(wc -l < "$sentinel" | tr -d ' ')
        echo "  sentinel exists: python3 was spawned $call_count time(s) (expected 0)"
        return 1
    fi
    return 0
}

# ── Test 2: write_commit_event output is byte-identical to Python's json.dumps ──
# Uses ensure_ascii=False, separators=(',',':'), sort_keys=True for comparison.
# Inputs include unicode (título), backslash, and double-quote characters.
#
# RED rationale: the current Python-backed implementation writes JSON via
# json.dump(data) with default spacing/key-ordering (e.g., ": " separators, no
# sort_keys), so the raw file bytes will NOT match the compact sorted canonical
# form.  The bash-native implementation MUST produce that canonical form.
test_write_commit_event_json_byte_exact() {
    local repo ticket_id sentinel shim_dir
    repo=$(_make_test_repo)
    ticket_id=$(cd "$repo" && _TICKET_TEST_NO_SYNC=1 bash "$TICKET_SCRIPT" create task "Byte-exact test" 2>/dev/null | tail -1)
    if [ -z "$ticket_id" ]; then
        echo "  setup failed: ticket create returned empty ID"
        return 1
    fi

    # Craft a title with unicode, backslash, and double-quote.
    local test_title='título \"back\\slash\"'

    # Build the event JSON via Python (helper uses python3 — fine here because
    # we are testing the *output* of write_commit_event, not its internals).
    local event_json
    event_json=$(_make_event_json "$repo" "$ticket_id" "$test_title")

    # Derive the expected canonical raw bytes once from the input data.
    local expected_raw
    expected_raw=$(python3 - "$event_json" <<'PYEOF'
import json, sys
with open(sys.argv[1], encoding='utf-8') as f:
    data = json.load(f)
# Canonical form: compact separators, sorted keys, no trailing newline.
import sys
sys.stdout.buffer.write(
    json.dumps(data, ensure_ascii=False, separators=(',', ':'), sort_keys=True).encode('utf-8')
)
PYEOF
    ) || {
        echo "  setup failed: python3 could not produce expected JSON"
        return 1
    }

    # Now call write_commit_event with a python3 sentinel so we can assert
    # whether python3 was invoked (the RED condition for the bash-native story).
    sentinel=$(mktemp)
    rm -f "$sentinel"
    _CLEANUP_FILES+=("$sentinel") 2>/dev/null || true
    shim_dir=$(_make_python3_shim "$sentinel")

    local exit_code=0
    (
        cd "$repo"
        PATH="$shim_dir:$PATH" _TICKET_TEST_NO_SYNC=1 bash -c "
            source '$TICKET_LIB'
            write_commit_event '$ticket_id' '$event_json'
        " 2>/dev/null
    ) || exit_code=$?

    if [ "$exit_code" -ne 0 ]; then
        echo "  write_commit_event exited $exit_code (expected 0)"
        return 1
    fi

    # Find the written event file.
    local tracker_dir="$repo/.tickets-tracker"
    local event_file
    event_file=$(find "$tracker_dir/$ticket_id" -maxdepth 1 -name '*-CREATE.json' ! -name '.*' 2>/dev/null | sort | tail -1)
    if [ -z "$event_file" ] || [ ! -f "$event_file" ]; then
        echo "  no CREATE event file found under $tracker_dir/$ticket_id"
        return 1
    fi

    # Read the raw bytes written by write_commit_event (no re-serialisation).
    local actual_raw
    actual_raw=$(python3 - "$event_file" <<'PYEOF'
import sys
sys.stdout.buffer.write(open(sys.argv[1], 'rb').read().rstrip(b'\n'))
PYEOF
    )

    # RED assertion 1: python3 must NOT have been spawned.
    if [ -f "$sentinel" ]; then
        local call_count
        call_count=$(wc -l < "$sentinel" | tr -d ' ')
        echo "  python3 was spawned $call_count time(s) — bash-native implementation required"
        return 1
    fi

    # RED assertion 2: raw bytes must match the canonical form exactly.
    if [ "$expected_raw" != "$actual_raw" ]; then
        echo "  JSON byte mismatch (current impl uses non-canonical separators/ordering)"
        echo "    expected (compact+sorted): $expected_raw"
        echo "    actual (current impl):     $actual_raw"
        return 1
    fi
    return 0
}

# ── Test 3: two concurrent write_commit_event calls produce valid, non-corrupt files
# RED rationale: the bash-native implementation must use flock without python3.
# The current Python-backed path spawns python3 processes per call; this test
# asserts zero python3 spawns during the concurrent writes AND that both event
# files are valid JSON (no corruption/interleaving).
test_write_commit_event_concurrent_no_corruption() {
    local repo ticket_id_a ticket_id_b event_json_a event_json_b
    repo=$(_make_test_repo)

    # Create two separate tickets so each gets its own event file.
    ticket_id_a=$(cd "$repo" && _TICKET_TEST_NO_SYNC=1 bash "$TICKET_SCRIPT" create task "Concurrent A" 2>/dev/null | tail -1)
    ticket_id_b=$(cd "$repo" && _TICKET_TEST_NO_SYNC=1 bash "$TICKET_SCRIPT" create task "Concurrent B" 2>/dev/null | tail -1)
    if [ -z "$ticket_id_a" ] || [ -z "$ticket_id_b" ]; then
        echo "  setup failed: ticket create returned empty IDs"
        return 1
    fi

    event_json_a=$(_make_event_json "$repo" "$ticket_id_a" "Concurrent A")
    event_json_b=$(_make_event_json "$repo" "$ticket_id_b" "Concurrent B")

    # Sentinel for python3 spawn counting.
    local sentinel
    sentinel=$(mktemp)
    rm -f "$sentinel"
    _CLEANUP_FILES+=("$sentinel") 2>/dev/null || true
    local shim_dir
    shim_dir=$(_make_python3_shim "$sentinel")

    local exit_a=0 exit_b=0

    # Launch both write_commit_event calls as background subshells with the shim.
    (
        cd "$repo" || exit 1
        PATH="$shim_dir:$PATH" _TICKET_TEST_NO_SYNC=1 bash -c "
            source '$TICKET_LIB'
            write_commit_event '$ticket_id_a' '$event_json_a'
        " 2>/dev/null
    ) &
    local pid_a=$!

    (
        cd "$repo" || exit 1
        PATH="$shim_dir:$PATH" _TICKET_TEST_NO_SYNC=1 bash -c "
            source '$TICKET_LIB'
            write_commit_event '$ticket_id_b' '$event_json_b'
        " 2>/dev/null
    ) &
    local pid_b=$!

    wait "$pid_a" || exit_a=$?
    wait "$pid_b" || exit_b=$?

    # Both calls must have succeeded (exit 0).
    if [ "$exit_a" -ne 0 ] || [ "$exit_b" -ne 0 ]; then
        echo "  concurrent write_commit_event failed: exit_a=$exit_a exit_b=$exit_b"
        return 1
    fi

    # RED assertion: python3 must NOT have been spawned by either call.
    if [ -f "$sentinel" ]; then
        local call_count
        call_count=$(wc -l < "$sentinel" | tr -d ' ')
        echo "  python3 was spawned $call_count time(s) during concurrent writes (expected 0)"
        return 1
    fi

    # Both event files must exist and be valid JSON (no corruption/interleaving).
    local tracker_dir="$repo/.tickets-tracker"
    local event_file_a event_file_b
    event_file_a=$(find "$tracker_dir/$ticket_id_a" -maxdepth 1 -name '*.json' ! -name '.*' 2>/dev/null | sort | tail -1)
    event_file_b=$(find "$tracker_dir/$ticket_id_b" -maxdepth 1 -name '*.json' ! -name '.*' 2>/dev/null | sort | tail -1)

    for f in "$event_file_a" "$event_file_b"; do
        if [ -z "$f" ] || [ ! -f "$f" ]; then
            echo "  event file missing for one of the concurrent writers"
            return 1
        fi
        if ! python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$f" 2>/dev/null; then
            echo "  event file is invalid JSON: $f"
            return 1
        fi
    done
    return 0
}

# ── Test 4: write_commit_event with lock held does not silently corrupt output ──
# Holds the flock lock in a background process, then calls write_commit_event
# with FLOCK_STAGE_COMMIT_TIMEOUT=2.  Verifies the function either waits and
# succeeds (with a valid event file) OR fails with a clear non-zero exit code
# and a diagnostic message — never silently corrupts or loses the event.
#
# RED rationale: the bash-native implementation is required to use shell-level
# flock (not python3 fcntl).  The current Python-backed path spawns python3
# for every flock acquire; this test asserts zero python3 spawns in addition
# to the behavioral no-corruption invariant.
test_write_commit_event_retry_on_locked_file() {
    local repo ticket_id event_json
    repo=$(_make_test_repo)
    ticket_id=$(cd "$repo" && _TICKET_TEST_NO_SYNC=1 bash "$TICKET_SCRIPT" create task "Lock-retry test" 2>/dev/null | tail -1)
    if [ -z "$ticket_id" ]; then
        echo "  setup failed: ticket create returned empty ID"
        return 1
    fi

    event_json=$(_make_event_json "$repo" "$ticket_id" "Lock-retry test")

    local tracker_dir
    tracker_dir=$(cd "$repo/.tickets-tracker" && pwd -P)
    local lock_file="$tracker_dir/.ticket-write.lock"

    # Hold the lock for 30 s using python3 fcntl (setup only — not under test).
    local lock_holder_pid
    (
        python3 -c "
import fcntl, os, time
fd = os.open('$lock_file', os.O_CREAT | os.O_RDWR)
fcntl.flock(fd, fcntl.LOCK_EX)
time.sleep(30)
os.close(fd)
" 2>/dev/null
    ) &
    lock_holder_pid=$!

    # Give the lock holder a moment to acquire the lock.
    sleep 0.3

    # Sentinel for python3 spawn counting by write_commit_event itself.
    local sentinel
    sentinel=$(mktemp)
    rm -f "$sentinel"
    _CLEANUP_FILES+=("$sentinel") 2>/dev/null || true
    local shim_dir
    shim_dir=$(_make_python3_shim "$sentinel")

    local exit_code=0
    local out_file
    out_file=$(mktemp)
    _CLEANUP_FILES+=("$out_file") 2>/dev/null || true

    # Use a short flock timeout so the test does not block for 30 s.
    (
        cd "$repo"
        PATH="$shim_dir:$PATH" FLOCK_STAGE_COMMIT_TIMEOUT=2 _TICKET_TEST_NO_SYNC=1 bash -c "
            source '$TICKET_LIB'
            write_commit_event '$ticket_id' '$event_json'
        " >"$out_file" 2>&1
    ) || exit_code=$?

    # Kill the lock holder.
    kill "$lock_holder_pid" 2>/dev/null || true
    wait "$lock_holder_pid" 2>/dev/null || true

    # The bash-native implementation must NOT silently succeed when it could not
    # acquire the lock — it must either return non-zero OR have written a valid
    # event file.  What is NOT acceptable is exit 0 with no event file.
    local event_file
    event_file=$(find "$tracker_dir/$ticket_id" -maxdepth 1 -name '*.json' ! -name '.*' 2>/dev/null | sort | tail -1)

    if [ "$exit_code" -eq 0 ]; then
        # Claimed success — event file must exist and be valid JSON.
        if [ -z "$event_file" ] || [ ! -f "$event_file" ]; then
            echo "  write_commit_event returned 0 but no event file was written (silent loss)"
            return 1
        fi
        if ! python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$event_file" 2>/dev/null; then
            echo "  write_commit_event returned 0 but event file is corrupt JSON: $event_file"
            return 1
        fi
    else
        # Non-zero exit — acceptable.  Verify it emitted a diagnostic message.
        if [ ! -s "$out_file" ]; then
            echo "  write_commit_event returned $exit_code with no stderr/stdout (silent fail)"
            return 1
        fi
    fi

    # RED assertion: python3 must NOT have been spawned by write_commit_event.
    # The shim wraps the real python3 so the lock-holder setup above still works,
    # but every write_commit_event-internal python3 call will be recorded.
    if [ -f "$sentinel" ]; then
        local call_count
        call_count=$(wc -l < "$sentinel" | tr -d ' ')
        echo "  python3 was spawned $call_count time(s) by write_commit_event (expected 0)"
        return 1
    fi

    return 0
}

# ── Test 5: write_commit_event returns 0 on success ───────────────────────────
test_write_commit_event_exit_codes() {
    local repo ticket_id event_json sentinel shim_dir
    repo=$(_make_test_repo)
    ticket_id=$(cd "$repo" && _TICKET_TEST_NO_SYNC=1 bash "$TICKET_SCRIPT" create task "Exit-code test" 2>/dev/null | tail -1)
    if [ -z "$ticket_id" ]; then
        echo "  setup failed: ticket create returned empty ID"
        return 1
    fi

    event_json=$(_make_event_json "$repo" "$ticket_id" "Exit-code test")

    sentinel=$(mktemp)
    rm -f "$sentinel"
    _CLEANUP_FILES+=("$sentinel") 2>/dev/null || true
    shim_dir=$(_make_python3_shim "$sentinel")

    local exit_code=0
    (
        cd "$repo"
        PATH="$shim_dir:$PATH" _TICKET_TEST_NO_SYNC=1 bash -c "
            source '$TICKET_LIB'
            write_commit_event '$ticket_id' '$event_json'
        " 2>/dev/null
    ) || exit_code=$?

    # RED: the current implementation spawns python3; the bash-native version
    # must return 0 AND have spawned zero python3 processes.
    if [ "$exit_code" -ne 0 ]; then
        echo "  write_commit_event exited $exit_code (expected 0)"
        return 1
    fi

    # The test is RED because the no-python3 requirement is violated.
    if [ -f "$sentinel" ]; then
        local call_count
        call_count=$(wc -l < "$sentinel" | tr -d ' ')
        echo "  python3 was spawned $call_count time(s) — bash-native implementation required"
        return 1
    fi

    return 0
}

# ── Test 6: 50-randomized-input JSON byte-exact test ─────────────────────────
# Generates ≥50 random title strings covering: double-quotes, backslashes,
# newlines, tabs, unicode (título, 中文, emoji).  For each input, creates a
# ticket, writes a CREATE event via write_commit_event, reads the resulting
# event file, and compares its md5 against Python's canonical json.dumps output.
# GREEN: byte-exact match is expected because write_commit_event uses jq -S -c
# which produces output identical to Python's
# json.dumps(ensure_ascii=False, separators=(',',':'), sort_keys=True).
test_write_commit_event_json_byte_exact_randomized() {
    local repo
    repo=$(_make_test_repo)

    # Generate ≥50 test titles covering edge cases.
    # Use a fixed seed-like array for reproducibility without relying on $RANDOM seeding.
    local titles=(
        'plain ascii title'
        'título con acento'
        '中文标题'
        'emoji 🎸🔥'
        'with "double quotes" inside'
        'back\\slash test'
        'tab	here'
        $'newline\nembedded'
        'both "quotes" and back\\slashes'
        'triple \"\"\" quotes'
        'unicode café résumé naïve'
        '日本語テスト'
        '한국어 테스트'
        'Ελληνικά'
        'mixed: "quoted" and \\escaped'
        'emoji combo 🎉🎊🎈'
        'zero-width\xe2\x80\x8bspace'
        'em—dash and en–dash'
        "curly \"quotes\" and apostrophe"
        'slash / and backslash \\ pair'
        'angle <brackets> & ampersand'
        'percent % sign 100%'
        'at @ symbol test'
        'hash # number sign'
        "dollar literal-sign expansion"
        "backtick literal-cmd test"
        'pipe | character'
        'semicolon ; separator'
        'null-like string (not really)'
        'very long title: ' + "$(python3 -c "print('x'*80)" 2>/dev/null || printf '%0.sx' {1..80})"
        'unicode snowman ☃'
        'musical note ♪♫'
        'copyright © and registered ®'
        'trademark ™ symbol'
        'degree 45° angle'
        'bullet • point'
        'ellipsis … three dots'
        'section § symbol'
        'paragraph ¶ mark'
        'checkmark ✓ and cross ✗'
        'left «guillemets» right'
        'fraction ½ and ¾'
        'superscript E=mc²'
        'subscript H₂O'
        'combining diacritics: â ê î ô û'
        'right-to-left: مرحبا'
        'hebrew: שלום'
        'cyrillic: привет'
        'mixed script: hello مرحبا 你好'
        'control-adjacent: \\t\\n\\r literal'
    )

    local total=0
    local mismatches=0
    local failures=0

    # Defer md5 hashing: collect (expected-input-json, actual-output-event) file
    # path pairs in the loop, then compute and compare every hash in ONE python3
    # process after the loop (was 2 python3 spawns per input → ~100 process
    # startups). The ticket-create and write_commit_event subprocesses — the
    # actual behavior under test — run exactly as before; only the redundant
    # per-input md5 interpreter startups are batched. _make_event_json uses
    # mktemp (unique, cleanup-registered paths), so the deferred reads are safe.
    local -a _exp_inputs=() _act_outputs=()

    for title in "${titles[@]}"; do
        total=$((total + 1))

        # Create a ticket for this input.
        local ticket_id
        ticket_id=$(cd "$repo" && _TICKET_TEST_NO_SYNC=1 bash "$TICKET_SCRIPT" create task "$title" 2>/dev/null | tail -1)
        if [ -z "$ticket_id" ]; then
            failures=$((failures + 1))
            continue
        fi

        local event_json
        event_json=$(_make_event_json "$repo" "$ticket_id" "$title")

        # Call write_commit_event (no shim — we only care about byte correctness here).
        local wce_exit=0
        (
            cd "$repo"
            _TICKET_TEST_NO_SYNC=1 bash -c "
                source '$TICKET_LIB'
                write_commit_event '$ticket_id' '$event_json'
            " 2>/dev/null
        ) || wce_exit=$?

        if [ "$wce_exit" -ne 0 ]; then
            failures=$((failures + 1))
            continue
        fi

        # Find the written event file.
        local tracker_dir="$repo/.tickets-tracker"
        local event_file
        event_file=$(find "$tracker_dir/$ticket_id" -maxdepth 1 -name '*-CREATE.json' ! -name '.*' 2>/dev/null | sort | tail -1)
        if [ -z "$event_file" ] || [ ! -f "$event_file" ]; then
            failures=$((failures + 1))
            continue
        fi

        _exp_inputs+=("$event_json")
        _act_outputs+=("$event_file")
    done

    # Batch md5: for each (input-json, output-event) pair, hash the expected
    # canonical JSON and the actual raw bytes (trailing newline stripped) and
    # count mismatches — identical per-input hashing to the prior inline form,
    # in a single interpreter. A read/parse error counts as a mismatch so any
    # problem still fails the test.
    local _pair_count=${#_exp_inputs[@]}
    if [ "$_pair_count" -gt 0 ]; then
        mismatches=$(python3 - "$_pair_count" "${_exp_inputs[@]}" "${_act_outputs[@]}" <<'PYEOF'
import json, sys, hashlib
n = int(sys.argv[1])
exp_files = sys.argv[2:2 + n]
act_files = sys.argv[2 + n:2 + 2 * n]
mism = 0
for ef, af in zip(exp_files, act_files):
    try:
        with open(ef, encoding='utf-8') as fh:
            data = json.load(fh)
        canonical = json.dumps(data, ensure_ascii=False, separators=(',', ':'), sort_keys=True)
        expected = hashlib.md5(canonical.encode('utf-8')).hexdigest()
        raw = open(af, 'rb').read().rstrip(b'\n')
        actual = hashlib.md5(raw).hexdigest()
        if expected != actual:
            mism += 1
    except Exception as exc:
        # Log the offending pair + error before counting it, so a fixture or
        # encoding problem is diagnosable rather than silently folded into the
        # mismatch total (the prior inline form surfaced these via a separate
        # 'failures' path). stderr is captured in the CI job log.
        sys.stderr.write(f"md5 batch error for {ef} / {af}: {exc!r}\n")
        mism += 1
print(mism)
PYEOF
        ) || mismatches=-1
    fi
    if [ "$mismatches" -lt 0 ]; then
        echo "  batch md5 computation failed"
        return 1
    fi

    if [ "$failures" -gt 0 ]; then
        echo "  $failures/$total inputs failed setup or write_commit_event"
        return 1
    fi
    if [ "$mismatches" -gt 0 ]; then
        echo "  $mismatches/$total inputs had JSON byte mismatches"
        return 1
    fi
    if [ "$total" -lt 50 ]; then
        echo "  only $total test inputs generated (expected ≥50)"
        return 1
    fi
    return 0
}

# ── Test 7: read-after-write consistency ─────────────────────────────────────
# Verifies that immediately after a ticket comment is written, ticket show
# reflects the comment in its JSON output, and ticket list shows the ticket.
# No stale cache should interfere (the event-sourced design has no read cache).
test_write_commit_event_read_after_write() {
    local repo
    repo=$(_make_test_repo)

    # Step 1: Create a ticket.
    local ticket_id
    ticket_id=$(cd "$repo" && _TICKET_TEST_NO_SYNC=1 bash "$TICKET_SCRIPT" create task "Read-after-write test" 2>/dev/null | tail -1)
    if [ -z "$ticket_id" ]; then
        echo "  setup failed: ticket create returned empty ID"
        return 1
    fi

    # Step 2: Add a comment via ticket comment.
    local comment_text
    comment_text="unique-comment-$(date +%s%N 2>/dev/null || date +%s)"
    local comment_exit=0
    (
        cd "$repo"
        _TICKET_TEST_NO_SYNC=1 bash "$TICKET_SCRIPT" comment "$ticket_id" "$comment_text" 2>/dev/null
    ) || comment_exit=$?

    if [ "$comment_exit" -ne 0 ]; then
        echo "  ticket comment failed with exit $comment_exit"
        return 1
    fi

    # Step 3: Immediately run ticket show and verify the comment appears.
    local show_output
    show_output=$(
        cd "$repo" || return 1
        _TICKET_TEST_NO_SYNC=1 bash "$TICKET_SCRIPT" show "$ticket_id" 2>/dev/null
    )

    if [ -z "$show_output" ]; then
        echo "  ticket show returned empty output"
        return 1
    fi

    if ! echo "$show_output" | python3 -c "import json,sys; json.load(sys.stdin)" 2>/dev/null; then
        echo "  ticket show output is not valid JSON"
        return 1
    fi

    # The comment text must appear somewhere in the show output.
    if ! echo "$show_output" | python3 -c "
import json, sys
data = json.load(sys.stdin)
comments = data.get('comments', [])
# comments may be a list of strings or a list of objects with a body/text field
found = False
for c in comments:
    if isinstance(c, str) and '$comment_text' in c:
        found = True
    elif isinstance(c, dict):
        for v in c.values():
            if isinstance(v, str) and '$comment_text' in v:
                found = True
if not found:
    sys.exit(1)
" 2>/dev/null; then
        echo "  comment '$comment_text' not found in ticket show output"
        echo "  show output (comments): $(echo "$show_output" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('comments','no-comments-key'))" 2>/dev/null || echo "$show_output")"
        return 1
    fi

    # Step 4: Run ticket list and verify the ticket appears.
    local list_output
    list_output=$(
        cd "$repo" || return 1
        _TICKET_TEST_NO_SYNC=1 bash "$TICKET_SCRIPT" list 2>/dev/null
    )

    if [ -z "$list_output" ]; then
        echo "  ticket list returned empty output"
        return 1
    fi

    # The ticket ID must appear in the list output.
    if ! grep -q "$ticket_id" <<< "$list_output" 2>/dev/null; then
        echo "  ticket $ticket_id not found in ticket list output"
        return 1
    fi

    return 0
}

# ── Test 11 (RED): write_commit_event accepts FILE_IMPACT event type ──────────
# RED: ticket-lib.sh does not yet list FILE_IMPACT in the allowed event_type enum.
# The test will FAIL (exit non-zero) until write_commit_event adds FILE_IMPACT to
# the case statement at the enum-validation step.
test_write_commit_event_accepts_file_impact() {
    local repo ticket_id
    repo=$(_make_test_repo)
    ticket_id=$(cd "$repo" && _TICKET_TEST_NO_SYNC=1 bash "$TICKET_SCRIPT" create task "FILE_IMPACT test" 2>/dev/null | tail -1)
    if [ -z "$ticket_id" ]; then
        echo "  setup failed: ticket create returned empty ID"
        return 1
    fi

    # Build a FILE_IMPACT event JSON file (same structure as _make_event_json but
    # with event_type=FILE_IMPACT and a file_impact list in data).
    local event_json
    event_json=$(mktemp)
    _CLEANUP_FILES+=("$event_json") 2>/dev/null || true
    python3 - "$event_json" "$ticket_id" <<'PYEOF'
import json, sys, uuid, datetime
out_path = sys.argv[1]
ticket_id = sys.argv[2]
event = {
    "event_type": "FILE_IMPACT",
    "timestamp": datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%S%f") + "Z",
    "uuid": str(uuid.uuid4()).replace("-", "")[:12],
    "data": {
        "file_impact": [{"path": "src/foo.py", "reason": "modified"}],
    },
}
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(event, f, ensure_ascii=False)
PYEOF

    # Call write_commit_event with the FILE_IMPACT event JSON file.
    # RED: this exits non-zero because FILE_IMPACT is not in the allowed enum yet.
    local exit_code=0
    (
        cd "$repo"
        _TICKET_TEST_NO_SYNC=1 bash -c "
            source '$TICKET_LIB'
            write_commit_event '$ticket_id' '$event_json'
        " 2>/dev/null
    ) || exit_code=$?

    assert_eq "write_commit_event must exit 0 for FILE_IMPACT event" "0" "$exit_code"

    # Assert: event file with -FILE_IMPACT.json suffix is written to the ticket dir.
    local tracker_dir="$repo/.tickets-tracker"
    local event_file
    event_file=$(find "$tracker_dir/$ticket_id" -name '*-FILE_IMPACT.json' 2>/dev/null | head -1)
    assert_ne "FILE_IMPACT event file must exist in ticket dir" "" "$event_file"

    # Only inspect file content when write_commit_event succeeded (GREEN state).
    if [ -n "$event_file" ] && [ -f "$event_file" ]; then
        local stored_type
        stored_type=$(python3 -c "import json; d=json.load(open('$event_file')); print(d.get('event_type',''))")
        assert_eq "stored event_type should be FILE_IMPACT" "FILE_IMPACT" "$stored_type"

        local has_fi
        has_fi=$(python3 -c "import json; d=json.load(open('$event_file')); print('yes' if 'file_impact' in d.get('data',{}) else 'no')")
        assert_eq "data must contain file_impact key" "yes" "$has_fi"
    fi
}

# ── Runner (tests 1-7) ─────────────────────────────────────────────────────────
pass=0
fail=0
for fn in \
    test_write_commit_event_no_python3 \
    test_write_commit_event_json_byte_exact \
    test_write_commit_event_concurrent_no_corruption \
    test_write_commit_event_retry_on_locked_file \
    test_write_commit_event_exit_codes \
    test_write_commit_event_json_byte_exact_randomized \
    test_write_commit_event_read_after_write
do
    if $fn; then
        echo "PASS: $fn"
        pass=$((pass + 1))
    else
        echo "FAIL: $fn"
        fail=$((fail + 1))
    fi
done

echo "Results (tests 1-7): $pass passed, $fail failed"

# ── Canonical write-path test (uses assert.sh) ───────────────────────────────

# ── Test 8: the canonical bash-native write path writes + commits the event ──
echo "Test 8: canonical write_commit_event writes the event and exits 0"
test_write_commit_event_canonical_path() {
    local repo
    repo=$(_make_test_repo)

    # Initialize ticket system
    (cd "$repo" && bash "$TICKET_SCRIPT" init 2>/dev/null) || true

    if [ ! -f "$TICKET_LIB" ]; then
        assert_eq "ticket-lib.sh exists" "exists" "missing"
        return
    fi

    # Create a python3 shim to detect spawns
    local shim_dir
    shim_dir=$(mktemp -d)
    _CLEANUP_DIRS+=("$shim_dir")

    local spawn_log="$shim_dir/python3_spawns.log"
    local real_python3
    real_python3=$(command -v python3)

    cat > "$shim_dir/python3" <<EOF
#!/usr/bin/env bash
echo "python3_spawn: \$*" >> "$spawn_log"
exec "$real_python3" "\$@"
EOF
    chmod +x "$shim_dir/python3"

    local ticket_id="test-noflag1"
    local tmpdir
    tmpdir=$(mktemp -d)
    _CLEANUP_DIRS+=("$tmpdir")
    local event_json="$tmpdir/event.json"
    _make_event_json_to_file "$event_json" "$ticket_id"

    # Run the canonical bash-native write path.
    # Use env to pass PATH into the subshell without the SC2030 warning.
    local exit_code=0
    # shellcheck disable=SC2016  # $1/$2/$3 are positional params for inner bash -c, not outer expansion
    (cd "$repo" && \
        env PATH="$shim_dir:$PATH" \
        bash -c 'source "$1" && write_commit_event "$2" "$3"' \
            _ "$TICKET_LIB" "$ticket_id" "$event_json") || exit_code=$?

    # Assert: operation still succeeds (existing behavior preserved)
    assert_eq "canonical write_commit_event exits 0" "0" "$exit_code"

    # Assert: event file was created (not broken by the guard)
    local event_files
    event_files=$(find "$repo/.tickets-tracker/$ticket_id" -maxdepth 1 \
        -name '*-CREATE.json' ! -name '.*' 2>/dev/null | wc -l | tr -d ' ')
    assert_eq "event file created on the canonical write path" "1" "$event_files"
}
test_write_commit_event_canonical_path

# ── Test 11 (RED): FILE_IMPACT event type written by write_commit_event ───────
echo "Test 11 (RED): write_commit_event writes FILE_IMPACT event file"
test_write_commit_event_accepts_file_impact

# ── Final summary (assert.sh counters) ─────────────────────────
print_summary

# Combined exit: both runner loop and assert.sh must pass
[ "$fail" -eq 0 ] && [ "${FAIL:-0}" -eq 0 ]
