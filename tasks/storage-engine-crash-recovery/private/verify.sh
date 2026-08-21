#!/usr/bin/env bash
# Private verifier for the ministore transaction-atomicity task.
#
# NOT solver facing.  It varies only what the task statement, README and
# FORMAT.md already say varies: which of the four documented fault points
# the power cut lands on, how far into the workload it lands, how big the
# transactions are relative to the buffer pool and the log budget, which
# kinds of operation they contain, whether some of them are aborted
# outright, and whether recovery is run again (or interrupted and run
# again) afterwards.
#
#   usage: verify.sh <path-to-built-tree> [-v]
#
# Exit status 0 only if every case passes.
set -u

TREE="${1:?usage: verify.sh <path-to-built-tree> [-v]}"
VERBOSE="${2:-}"
KVCLI="$TREE/build/kvcli"
WORK="${MINISTORE_VERIFY_DIR:-/tmp/ms_verify}"

[ -x "$KVCLI" ] || { echo "no kvcli at $KVCLI" >&2; exit 2; }
rm -rf "$WORK"; mkdir -p "$WORK"

PASS=0; FAIL=0
declare -a FAILED_CASES=()

note() { [ -n "$VERBOSE" ] && echo "     $*"; return 0; }

record() {                       # record <ok|no> <name> <detail>
    if [ "$1" = ok ]; then
        PASS=$((PASS + 1))
        [ -n "$VERBOSE" ] && echo "PASS $2"
    else
        FAIL=$((FAIL + 1))
        FAILED_CASES+=("$2")
        echo "FAIL $2"
        [ -n "$3" ] && echo "     $3"
    fi
    return 0
}

# base <dir> <keys>  - a filled, cleanly closed database to start from.
# Filling is the slow part and the result depends only on the key count,
# so each size is built once and copied for every case that wants it.
base() {
    local d="$1" keys="$2"
    local cache="$WORK/_base_$keys"
    mkdir -p "$d"
    if [ ! -f "$cache/base.db" ]; then
        mkdir -p "$cache"
        if [ "$keys" -eq 0 ]; then
            "$KVCLI" recover "$cache/base.db" >/dev/null 2>&1 || return 1
        else
            "$KVCLI" fill "$cache/base.db" "$keys" >/dev/null 2>&1 || return 1
        fi
    fi
    cp "$cache/base.db" "$d/base.db" || return 1
    cp "$cache/base.db.wal" "$d/base.db.wal" 2>/dev/null || true
    return 0
}

# ---------------------------------------------------------------------
# one_case <name> <base-keys> <point> <count> <verb> <workload options...>
#
# Runs the workload under a scripted power cut, then reopens the database
# (which recovers) and checks that no transaction is half applied, that
# every transaction whose commit returned is there in full, and that
# running recovery again changes nothing.
# ---------------------------------------------------------------------
one_case() {
    local name="$1" keys="$2" point="$3" count="$4" verb="$5"; shift 5
    local d="$WORK/$name"
    rm -rf "$d"; mkdir -p "$d"

    if ! base "$d" "$keys"; then
        record no "$name" "could not build the starting database"
        return
    fi
    cp "$d/base.db" "$d/a.db"
    cp "$d/base.db.wal" "$d/a.db.wal" 2>/dev/null || true

    MINISTORE_CRASH_POINT="$point" MINISTORE_CRASH_COUNT="$count" \
        "$KVCLI" "$verb" "$d/a.db" "$@" --progress "$d/p" >/dev/null 2>&1
    local rc=$?
    if [ "$rc" -ne 0 ] && [ "$rc" -ne 90 ]; then
        record no "$name" "the workload died with exit $rc"
        return
    fi
    local cm; cm="$(tail -1 "$d/p" 2>/dev/null)"
    case "$cm" in ''|*[!0-9]*) cm=0 ;; esac
    cm=$((10#$cm))
    # An engine may legitimately never reach the scripted fault: one
    # that writes no page while a transaction is running has no page
    # write to be stopped at.  That is not a failure by itself.  The
    # workload simply ran to the end, and what it left behind still
    # has to be right.

    local out
    if ! out="$("$KVCLI" "expect-$verb" "$d/a.db" "$@" --committed "$cm" 2>&1)"; then
        record no "$name" "$out"
        return
    fi
    note "$out"

    # recovery has to be repeatable
    local h1 h2
    h1="$("$KVCLI" dump "$d/a.db" | sha256sum)"
    "$KVCLI" recover "$d/a.db" >/dev/null 2>&1
    h2="$("$KVCLI" dump "$d/a.db" | sha256sum)"
    if [ "$h1" != "$h2" ]; then
        record no "$name" "running recovery again changed the database"
        return
    fi
    record ok "$name" ""
}

# ---------------------------------------------------------------------
# interrupted_case: the power cut lands inside the workload, and then the
# recovery that follows is itself interrupted, twice, before a final
# attempt is allowed to finish.
# ---------------------------------------------------------------------
interrupted_case() {
    local name="$1" keys="$2" point="$3" count="$4" verb="$5"; shift 5
    local d="$WORK/$name"
    rm -rf "$d"; mkdir -p "$d"

    if ! base "$d" "$keys"; then
        record no "$name" "could not build the starting database"
        return
    fi
    cp "$d/base.db" "$d/a.db"
    cp "$d/base.db.wal" "$d/a.db.wal" 2>/dev/null || true

    MINISTORE_CRASH_POINT="$point" MINISTORE_CRASH_COUNT="$count" \
        "$KVCLI" "$verb" "$d/a.db" "$@" --progress "$d/p" >/dev/null 2>&1
    local rc=$?
    if [ "$rc" -ne 0 ] && [ "$rc" -ne 90 ]; then
        record no "$name" "the workload died with exit $rc"
        return
    fi
    local cm; cm="$(tail -1 "$d/p" 2>/dev/null)"
    case "$cm" in ''|*[!0-9]*) cm=0 ;; esac
    cm=$((10#$cm))

    # two recoveries that are themselves cut short
    for attempt in 4 11; do
        MINISTORE_CRASH_POINT=page_write MINISTORE_CRASH_COUNT="$attempt" \
            "$KVCLI" recover "$d/a.db" >/dev/null 2>&1
    done

    local out
    if ! out="$("$KVCLI" "expect-$verb" "$d/a.db" "$@" --committed "$cm" 2>&1)"; then
        record no "$name" "$out"
        return
    fi
    local h1 h2
    h1="$("$KVCLI" dump "$d/a.db" | sha256sum)"
    "$KVCLI" recover "$d/a.db" >/dev/null 2>&1
    h2="$("$KVCLI" dump "$d/a.db" | sha256sum)"
    if [ "$h1" != "$h2" ]; then
        record no "$name" "running recovery again changed the database"
        return
    fi
    record ok "$name" ""
}

echo "== ministore private verifier: $TREE"

# Crash counts are calibrated against the shipped engine, which performs
# the fewest page writes of any build being graded, so every case really
# does stop where it says it does.
#
#   rewrite 10x20  over    400 keys     29 page writes
#   rewrite 10x150 over   1500 keys   1500 page writes
#   rewrite  5x400 over   2000 keys   1427 page writes
#   batch   10x150 (from empty)       1257 page writes

# --- A/B: a transaction that fits in the buffer pool, and ones that do not
one_case small_pool_writeback 400  page_write 8   rewrite --txns 10 --size 20
one_case small_pool_late      400  page_write 24  rewrite --txns 10 --size 20
one_case small_pool_commit    400  commit     4   rewrite --txns 10 --size 20
one_case wide_writeback_a     1500 page_write 120  rewrite --txns 10 --size 150
one_case wide_writeback_b     1500 page_write 340  rewrite --txns 10 --size 150
one_case wide_writeback_c     1500 page_write 610  rewrite --txns 10 --size 150
one_case wide_writeback_d     1500 page_write 880  rewrite --txns 10 --size 150
one_case wide_writeback_e     1500 page_write 1300 rewrite --txns 10 --size 150
one_case wide_commit          1500 commit     6    rewrite --txns 10 --size 150
one_case wide_meta            1500 meta_write 2    rewrite --txns 10 --size 150

# --- C: aborts interleaved with the crashed workload
one_case aborts_writeback     1500 page_write 260  rewrite --txns 10 --size 150 --abort-every 3
one_case aborts_late          1500 page_write 700  rewrite --txns 10 --size 150 --abort-every 3
one_case aborts_checkpoint    1500 checkpoint 1    rewrite --txns 10 --size 150 --abort-every 4
one_case aborts_meta          1500 meta_write 2    rewrite --txns 10 --size 150 --abort-every 5

# --- E: one transaction containing inserts, updates and deletes
one_case mixed_writeback_a    1500 page_write 200  rewrite --txns 10 --size 150 --delete-every 5
one_case mixed_writeback_b    1500 page_write 520  rewrite --txns 10 --size 150 --delete-every 5
one_case mixed_writeback_c    1500 page_write 1000 rewrite --txns 10 --size 150 --delete-every 5
one_case mixed_full_a         1500 page_write 260  rewrite --txns 10 --size 150 --delete-every 5 --insert-every 7
one_case mixed_full_b         1500 page_write 900  rewrite --txns 10 --size 150 --delete-every 5 --insert-every 7
one_case mixed_full_ckpt      1500 checkpoint 1    rewrite --txns 10 --size 150 --delete-every 5 --insert-every 7

# --- D: transactions that split internal nodes and grow the root
one_case grow_writeback_a     0    page_write 150  batch --txns 10 --size 150
one_case grow_writeback_b     0    page_write 400  batch --txns 10 --size 150
one_case grow_writeback_c     0    page_write 750  batch --txns 10 --size 150
one_case grow_writeback_d     0    page_write 1100 batch --txns 10 --size 150
one_case grow_checkpoint      0    checkpoint 2    batch --txns 10 --size 150
one_case grow_commit          0    commit     5    batch --txns 10 --size 150
one_case grow_aborts          0    page_write 600  batch --txns 10 --size 150 --abort-every 3

# --- F/I: the log budget forces checkpoints part way through, so pages
#          are published and records recycled while a transaction is live
one_case budget_ckpt_1        2000 checkpoint 1    rewrite --txns 5 --size 400
one_case budget_ckpt_2        2000 checkpoint 2    rewrite --txns 5 --size 400
one_case budget_ckpt_3        2000 checkpoint 4    rewrite --txns 5 --size 400
one_case budget_writeback_a   2000 page_write 200  rewrite --txns 5 --size 400
one_case budget_writeback_b   2000 page_write 560  rewrite --txns 5 --size 400
one_case budget_writeback_c   2000 page_write 940  rewrite --txns 5 --size 400
one_case budget_writeback_d   2000 page_write 1300 rewrite --txns 5 --size 400
one_case budget_meta          2000 meta_write 3    rewrite --txns 5 --size 400

# --- G/H: recovery repeated, and recovery interrupted part way and re-run
interrupted_case undo_interrupted_a 1500 page_write 400  rewrite --txns 10 --size 150
interrupted_case undo_interrupted_b 1500 page_write 800  rewrite --txns 10 --size 150 --delete-every 5
interrupted_case undo_interrupted_c 2000 checkpoint 1    rewrite --txns 5  --size 400
interrupted_case undo_interrupted_d 0    page_write 700  batch --txns 10 --size 150

# --- clean-path controls: none of this may break ordinary running
d="$WORK/clean"; mkdir -p "$d"
if "$KVCLI" fill "$d/a.db" 1500 >/dev/null 2>&1 &&
   "$KVCLI" rewrite "$d/a.db" --txns 10 --size 150 --abort-every 3 \
        --delete-every 5 --insert-every 7 >/dev/null 2>&1 &&
   out="$("$KVCLI" expect-rewrite "$d/a.db" --txns 10 --size 150 \
        --abort-every 3 --delete-every 5 --insert-every 7 --committed 10 2>&1)"; then
    record ok clean_mixed_with_aborts ""
else
    record no clean_mixed_with_aborts "${out:-workload failed}"
fi

d="$WORK/clean_big"; mkdir -p "$d"
if "$KVCLI" fill "$d/a.db" 1500 >/dev/null 2>&1 &&
   "$KVCLI" rewrite "$d/a.db" --txns 3 --size 480 >/dev/null 2>&1 &&
   out="$("$KVCLI" expect-rewrite "$d/a.db" --txns 3 --size 480 \
        --committed 3 2>&1)"; then
    record ok clean_transaction_larger_than_the_log_budget ""
else
    record no clean_transaction_larger_than_the_log_budget "${out:-workload failed}"
fi

# --- the engine's own suite still has to pass
if (cd "$TREE" && tests/run_all.sh >/dev/null 2>&1); then
    record ok public_suite ""
else
    record no public_suite "the repository's own tests do not all pass"
fi

echo "---- private verifier: $PASS passed, $FAIL failed"
if [ "$FAIL" -ne 0 ]; then
    printf '     %s\n' "${FAILED_CASES[@]}"
    exit 1
fi
exit 0
