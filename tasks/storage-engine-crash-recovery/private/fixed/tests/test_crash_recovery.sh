#!/usr/bin/env bash
# Crash recovery.
#
# Every write whose commit returned must come back after a power cut, the
# writes that never committed must not, and the tree that comes back must
# be a well formed B+tree.  Each case kills the writer the instant a
# scripted commit becomes durable, then reopens the database - which runs
# recovery - and checks exactly which keys survived.
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
setup_suite

# ---- databases small enough to sit entirely in the buffer pool --------
db="$(fresh_db crash_small_seq)"
if crash_fill_opts "$db" 300 150 ""; then
    ok "small database, keys in order: recovers" \
       "$KVCLI" expect "$db" 300 150
else
    echo "FAIL small database, keys in order: workload did not crash"
    FAILED=$((FAILED + 1))
fi

db="$(fresh_db crash_small_spread)"
if crash_fill_opts "$db" 350 180 "--spread"; then
    ok "small database, keys scattered: recovers" \
       "$KVCLI" expect "$db" 350 180 --spread
else
    echo "FAIL small database, keys scattered: workload did not crash"
    FAILED=$((FAILED + 1))
fi

# ---- databases larger than the buffer pool ----------------------------
db="$(fresh_db crash_large_seq)"
if crash_fill_opts "$db" 3000 1500 ""; then
    ok "large database, keys in order: recovers" \
       "$KVCLI" expect "$db" 3000 1500
else
    echo "FAIL large database, keys in order: workload did not crash"
    FAILED=$((FAILED + 1))
fi

db="$(fresh_db crash_large_spread)"
if crash_fill_opts "$db" 3000 1200 "--spread"; then
    ok "large database, keys scattered: recovers" \
       "$KVCLI" expect "$db" 3000 1200 --spread
else
    echo "FAIL large database, keys scattered: workload did not crash"
    FAILED=$((FAILED + 1))
fi

# ---- with checkpoints along the way -----------------------------------
db="$(fresh_db crash_checkpointed)"
MINISTORE_CRASH_POINT=commit MINISTORE_CRASH_COUNT=1800 \
    "$KVCLI" fill "$db" 2500 --spread --checkpoint-every 400 >/dev/null 2>&1
if [ "$?" -eq 90 ]; then
    ok "checkpoints during the workload: recovers" \
       "$KVCLI" expect "$db" 2500 1800 --spread
else
    echo "FAIL checkpoints during the workload: workload did not crash"
    FAILED=$((FAILED + 1))
fi

# ---- crash while pages are being written back -------------------------
db="$(fresh_db crash_mid_writeback)"
MINISTORE_CRASH_POINT=page_write MINISTORE_CRASH_COUNT=60 \
    "$KVCLI" fill "$db" 2500 --spread >/dev/null 2>&1
if [ "$?" -eq 90 ]; then
    ok "crash during page write back: tree is sound" "$KVCLI" verify "$db"
else
    echo "FAIL crash during page write back: workload did not crash"
    FAILED=$((FAILED + 1))
fi

# ---- a second crash on top of a recovered database --------------------
db="$(fresh_db crash_twice)"
if crash_fill_opts "$db" 3000 900 "--spread"; then
    "$KVCLI" recover "$db" >/dev/null 2>&1
    MINISTORE_CRASH_POINT=commit MINISTORE_CRASH_COUNT=400 \
        "$KVCLI" fill "$db" 1000 --start 4001 >/dev/null 2>&1
    ok "crash, recover, crash again: tree is sound" "$KVCLI" verify "$db"
else
    echo "FAIL crash, recover, crash again: workload did not crash"
    FAILED=$((FAILED + 1))
fi

# ---- work that never committed must not appear ------------------------
db="$(fresh_db crash_no_phantoms)"
if crash_fill_opts "$db" 3000 700 "--spread"; then
    ok "no phantom keys: exactly the committed set" \
       "$KVCLI" expect "$db" 3000 700 --spread
else
    echo "FAIL no phantom keys: workload did not crash"
    FAILED=$((FAILED + 1))
fi

finish_suite
