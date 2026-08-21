#!/usr/bin/env bash
# Crash recovery: every committed write must come back after a power cut,
# and the tree that comes back must be a well formed B+tree.
#
# Each case kills the writer the instant a scripted commit becomes durable,
# then reopens the database (which runs recovery) and checks the result.
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
setup_suite

# ---- the replayed window sits on top of a checkpointed tree ------------
db="$(fresh_db crash_after_ckpt_small)"
"$KVCLI" fill "$db" 100 >/dev/null
if crash_fill "$db" 80 101 45; then
    ok "crash after a checkpoint, small tree: recovers" \
       "$KVCLI" verify "$db" 145
else
    echo "FAIL crash after a checkpoint, small tree: workload did not crash"
    FAILED=$((FAILED + 1))
fi

db="$(fresh_db crash_after_ckpt_large)"
"$KVCLI" fill "$db" 1500 >/dev/null
if crash_fill "$db" 900 1501 600; then
    ok "crash after a checkpoint, large tree: recovers" \
       "$KVCLI" verify "$db" 2100
else
    echo "FAIL crash after a checkpoint, large tree: workload did not crash"
    FAILED=$((FAILED + 1))
fi

# ---- the replayed window covers the whole life of the tree -------------
db="$(fresh_db crash_from_empty_small)"
if crash_fill "$db" 100 1 40; then
    ok "crash on a young database: recovers" \
       "$KVCLI" verify "$db" 40
else
    echo "FAIL crash on a young database: workload did not crash"
    FAILED=$((FAILED + 1))
fi

db="$(fresh_db crash_from_empty_medium)"
if crash_fill "$db" 600 1 300; then
    ok "crash while the tree is growing: recovers" \
       "$KVCLI" verify "$db" 300
else
    echo "FAIL crash while the tree is growing: workload did not crash"
    FAILED=$((FAILED + 1))
fi

db="$(fresh_db crash_from_empty_large)"
if crash_fill "$db" 3000 1 2400; then
    ok "crash on a large growing tree: recovers" \
       "$KVCLI" verify "$db" 2400
else
    echo "FAIL crash on a large growing tree: workload did not crash"
    FAILED=$((FAILED + 1))
fi

# ---- crash with page write back already in progress -------------------
db="$(fresh_db crash_mid_writeback)"
MINISTORE_CRASH_POINT=page_write MINISTORE_CRASH_COUNT=25 \
    "$KVCLI" fill "$db" 4000 --checkpoint-every 500 >/dev/null 2>&1
rc=$?
if [ "$rc" -eq 90 ]; then
    ok "crash during page write back: tree is sound" "$KVCLI" verify "$db"
else
    echo "FAIL crash during page write back: workload did not crash (exit $rc)"
    FAILED=$((FAILED + 1))
fi

# ---- crash after updates and deletes ----------------------------------
db="$(fresh_db crash_mixed)"
"$KVCLI" fill "$db" 800 >/dev/null
"$KVCLI" del "$db" 5 >/dev/null
"$KVCLI" del "$db" 500 >/dev/null
if crash_fill "$db" 900 801 700; then
    ok "crash after a mixed workload: tree is sound" "$KVCLI" verify "$db"
    n="$("$KVCLI" stats "$db" | sed -n "s/keys=\([0-9]*\).*/\1/p")"
    same "crash after a mixed workload: committed keys survive" \
         "yes" "$([ "${n:-0}" -eq 1498 ] && echo yes || echo no)"
else
    echo "FAIL crash after a mixed workload: workload did not crash"
    FAILED=$((FAILED + 1))
fi

# ---- writes that were never committed must not appear ------------------
db="$(fresh_db crash_no_phantoms)"
if crash_fill "$db" 500 1 200; then
    ok "no phantom keys: tree is sound" "$KVCLI" verify "$db" 200
    fails "no phantom keys: uncommitted key absent" "$KVCLI" get "$db" 400
else
    echo "FAIL no phantom keys: workload did not crash"
    FAILED=$((FAILED + 1))
fi

finish_suite
