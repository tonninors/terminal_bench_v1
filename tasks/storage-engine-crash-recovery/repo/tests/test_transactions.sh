#!/usr/bin/env bash
# Multi-operation transactions.
#
# A transaction may contain many inserts, updates and deletes over many
# pages.  Either all of its operations are in the database or none of
# them are, and that has to hold after a crash and recovery just as it
# holds while the process is alive.
#
# These are representative cases, not every combination.
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
setup_suite

# A transaction of 200 operations touches far more than the 32 pages the
# buffer pool holds, and writes more log than the log budget allows, so
# the engine is forced to write its pages back and recycle its log before
# it knows whether the transaction will commit.
BIG="--txns 10 --size 150"

base() { filled_db "$1" 1500; }

# ---- while the process is alive ---------------------------------------
db="$(base txn_commit_clean)"
ok "committed transactions: every operation is there" \
   bash -c "'$KVCLI' rewrite '$db' $BIG >/dev/null && \
            '$KVCLI' expect-rewrite '$db' $BIG --committed 20"

db="$(base txn_abort_clean)"
ok "aborted transactions: nothing of them is there" \
   bash -c "'$KVCLI' rewrite '$db' $BIG --abort-every 3 >/dev/null && \
            '$KVCLI' expect-rewrite '$db' $BIG --committed 20 --abort-every 3"

db="$(base txn_mixed_clean)"
ok "one transaction inserting, updating and deleting" \
   bash -c "'$KVCLI' rewrite '$db' $BIG --delete-every 5 --insert-every 7 \
                >/dev/null && \
            '$KVCLI' expect-rewrite '$db' $BIG --delete-every 5 \
                --insert-every 7 --committed 20"

db="$(fresh_db txn_grow_clean)"
ok "transactions that grow the tree several levels" \
   bash -c "'$KVCLI' batch '$db' --txns 10 --size 150 >/dev/null && \
            '$KVCLI' expect-batch '$db' --txns 10 --size 150 --committed 10"

# ---- interrupted by a power cut ---------------------------------------
# Killed while the buffer pool is writing a frame back.  The transaction
# that was running had already had pages written underneath it.
db="$(base txn_crash_writeback)"
if crash_txn "$db" page_write 500 $BIG; then
    ok "power cut during write-back: no transaction is half applied" \
       "$KVCLI" expect-rewrite "$db" $BIG --committed "$CRASH_COMMITTED"
else
    echo "FAIL power cut during write-back: workload did not crash"
    FAILED=$((FAILED + 1))
fi

# Killed at the checkpoint the log budget forces part way through a
# transaction, after that checkpoint has recycled the log.
db="$(base txn_crash_checkpoint)"
if crash_txn "$db" checkpoint 1 $BIG; then
    ok "power cut at a checkpoint inside a transaction" \
       "$KVCLI" expect-rewrite "$db" $BIG --committed "$CRASH_COMMITTED"
else
    echo "FAIL power cut at a checkpoint inside a transaction: no crash"
    FAILED=$((FAILED + 1))
fi

# Killed the instant a commit became durable: that transaction is
# committed, so all of it has to come back.
db="$(base txn_crash_commit)"
if crash_txn "$db" commit 6 $BIG; then
    ok "power cut just after a commit: that transaction is whole" \
       "$KVCLI" expect-rewrite "$db" $BIG --committed "$CRASH_COMMITTED"
else
    echo "FAIL power cut just after a commit: workload did not crash"
    FAILED=$((FAILED + 1))
fi

# An interrupted transaction that was inserting, so it had split pages
# and moved keys between them.  None of that may survive either.
db="$(base txn_crash_growing)"
if crash_txn "$db" page_write 450 $BIG --delete-every 5 --insert-every 7; then
    ok "power cut in a transaction that was splitting pages" \
       "$KVCLI" expect-rewrite "$db" $BIG --delete-every 5 --insert-every 7 \
       --committed "$CRASH_COMMITTED"
else
    echo "FAIL power cut in a transaction that was splitting pages: no crash"
    FAILED=$((FAILED + 1))
fi

# ---- recovery is repeatable -------------------------------------------
db="$(base txn_crash_idem)"
if crash_txn "$db" page_write 700 $BIG; then
    ok  "after a transaction crash: recovery is sound" \
        "$KVCLI" expect-rewrite "$db" $BIG --committed "$CRASH_COMMITTED"
    first="$("$KVCLI" dump "$db" | sha256sum)"
    ok  "after a transaction crash: recovery runs again" \
        "$KVCLI" recover "$db"
    second="$("$KVCLI" dump "$db" | sha256sum)"
    same "after a transaction crash: the second pass changes nothing" \
         "$first" "$second"
else
    echo "FAIL after a transaction crash: workload did not crash"
    FAILED=$((FAILED + 1))
fi

finish_suite
