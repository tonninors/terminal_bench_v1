#!/usr/bin/env bash
# Shared helpers for the ministore test suite.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KVCLI="$ROOT/build/kvcli"
WORK="${MINISTORE_TEST_DIR:-$ROOT/build/testwork}"

PASSED=0
FAILED=0

setup_suite() {
    if [ ! -x "$KVCLI" ]; then
        echo "build/kvcli is missing; run make first" >&2
        exit 2
    fi
    rm -rf "$WORK"
    mkdir -p "$WORK"
}

db_path() { echo "$WORK/$1.db"; }

fresh_db() {
    local p
    p="$(db_path "$1")"
    rm -f "$p" "$p.wal"
    echo "$p"
}

# ok <name> <command...>   - the command must succeed
ok() {
    local name="$1"; shift
    local out
    if out="$("$@" 2>&1)"; then
        echo "PASS $name"
        PASSED=$((PASSED + 1))
    else
        echo "FAIL $name"
        echo "     command: $*"
        echo "$out" | sed "s/^/     /"
        FAILED=$((FAILED + 1))
    fi
}

# fails <name> <command...> - the command must NOT succeed
fails() {
    local name="$1"; shift
    if "$@" >/dev/null 2>&1; then
        echo "FAIL $name (expected a failure, got success)"
        FAILED=$((FAILED + 1))
    else
        echo "PASS $name"
        PASSED=$((PASSED + 1))
    fi
}

# same <name> <expected> <actual>
same() {
    local name="$1" want="$2" got="$3"
    if [ "$want" = "$got" ]; then
        echo "PASS $name"
        PASSED=$((PASSED + 1))
    else
        echo "FAIL $name"
        echo "     expected: $want"
        echo "     actual:   $got"
        FAILED=$((FAILED + 1))
    fi
}

# crash_fill <db> <count> <start> <crash_at_commit>
# Runs a fill that is killed the moment the given commit becomes durable.
crash_fill() {
    local db="$1" count="$2" start="$3" at="$4"
    MINISTORE_CRASH_POINT=commit MINISTORE_CRASH_COUNT="$at" \
        "$KVCLI" fill "$db" "$count" --start "$start" >/dev/null 2>&1
    local rc=$?
    if [ "$rc" -ne 90 ]; then
        echo "     (workload did not crash as scripted: exit $rc)" >&2
        return 1
    fi
    return 0
}

finish_suite() {
    echo "---- $(basename "$0"): $PASSED passed, $FAILED failed"
    [ "$FAILED" -eq 0 ]
}
