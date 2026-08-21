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

# filled_db <name> <keys> - a filled, cleanly closed database.  Filling is
# the slow part and depends only on the key count, so each size is built
# once and copied for every case that asks for it.
filled_db() {
    local name="$1" keys="$2" p cache
    p="$(db_path "$name")"
    cache="$WORK/_base_$keys.db"
    rm -f "$p" "$p.wal"
    if [ ! -f "$cache" ]; then
        "$KVCLI" fill "$cache" "$keys" >/dev/null 2>&1 || return 1
    fi
    cp "$cache" "$p"
    cp "$cache.wal" "$p.wal" 2>/dev/null || true
    echo "$p"
}

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

# crash_fill_opts <db> <count> <crash_at_commit> <extra fill options>
crash_fill_opts() {
    local db="$1" count="$2" at="$3" opts="$4"
    # shellcheck disable=SC2086
    MINISTORE_CRASH_POINT=commit MINISTORE_CRASH_COUNT="$at" \
        "$KVCLI" fill "$db" "$count" $opts >/dev/null 2>&1
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

# crash_txn <db> <point> <count> <rewrite options...>
# Runs a transaction workload that is killed at the given fault point.
# The database must already exist.  Leaves the number of transactions
# whose commit returned in $CRASH_COMMITTED.
CRASH_COMMITTED=0
crash_txn() {
    local db="$1" point="$2" count="$3"; shift 3
    local prog="$db.progress"
    rm -f "$prog"
    MINISTORE_CRASH_POINT="$point" MINISTORE_CRASH_COUNT="$count" \
        "$KVCLI" rewrite "$db" "$@" --progress "$prog" >/dev/null 2>&1
    local rc=$?
    local n
    n="$(tail -1 "$prog" 2>/dev/null)"
    case "$n" in ''|*[!0-9]*) n=0 ;; esac
    CRASH_COMMITTED=$((10#$n))
    if [ "$rc" -ne 90 ]; then
        echo "     (workload did not crash as scripted: exit $rc)" >&2
        return 1
    fi
    return 0
}

# crash_batch <db> <point> <count> <batch options...>
crash_batch() {
    local db="$1" point="$2" count="$3"; shift 3
    local prog="$db.progress"
    rm -f "$prog"
    MINISTORE_CRASH_POINT="$point" MINISTORE_CRASH_COUNT="$count" \
        "$KVCLI" batch "$db" "$@" --progress "$prog" >/dev/null 2>&1
    local rc=$?
    local n
    n="$(tail -1 "$prog" 2>/dev/null)"
    case "$n" in ''|*[!0-9]*) n=0 ;; esac
    CRASH_COMMITTED=$((10#$n))
    if [ "$rc" -ne 90 ]; then
        echo "     (workload did not crash as scripted: exit $rc)" >&2
        return 1
    fi
    return 0
}
