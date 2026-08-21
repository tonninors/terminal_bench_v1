#!/usr/bin/env bash
# Recovery must be repeatable.  Opening a database that has already been
# recovered - or recovering one that was closed cleanly - must leave the
# contents exactly as they were, however many times it happens.
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
setup_suite

# ---- recovering the same crashed database several times ---------------
db="$(fresh_db idem_repeat)"
if crash_fill "$db" 1200 1 900; then
    ok  "repeated recovery: first pass is sound" "$KVCLI" verify "$db" 900
    first="$("$KVCLI" dump "$db" | sha256sum)"
    ok  "repeated recovery: second pass"         "$KVCLI" recover "$db"
    ok  "repeated recovery: still sound"         "$KVCLI" verify "$db" 900
    second="$("$KVCLI" dump "$db" | sha256sum)"
    ok  "repeated recovery: third pass"          "$KVCLI" recover "$db"
    third="$("$KVCLI" dump "$db" | sha256sum)"
    same "repeated recovery: pass 2 matches pass 1" "$first" "$second"
    same "repeated recovery: pass 3 matches pass 1" "$first" "$third"
else
    echo "FAIL repeated recovery: workload did not crash"
    FAILED=$((FAILED + 1))
fi

# ---- recovery must not double apply anything --------------------------
db="$(fresh_db idem_pagecount)"
if crash_fill "$db" 900 1 700; then
    ok  "no double apply: sound after recovery" "$KVCLI" verify "$db" 700
    p1="$("$KVCLI" stats "$db" | sed -n "s/.*pages=\([0-9]*\).*/\1/p")"
    k1="$("$KVCLI" stats "$db" | sed -n "s/keys=\([0-9]*\).*/\1/p")"
    "$KVCLI" recover "$db" >/dev/null
    "$KVCLI" recover "$db" >/dev/null
    p2="$("$KVCLI" stats "$db" | sed -n "s/.*pages=\([0-9]*\).*/\1/p")"
    k2="$("$KVCLI" stats "$db" | sed -n "s/keys=\([0-9]*\).*/\1/p")"
    same "no double apply: page count stable" "$p1" "$p2"
    same "no double apply: key count stable"  "$k1" "$k2"
    ok   "no double apply: still sound"       "$KVCLI" verify "$db" 700
else
    echo "FAIL no double apply: workload did not crash"
    FAILED=$((FAILED + 1))
fi

# ---- a crash during recovery itself -----------------------------------
db="$(fresh_db idem_crash_in_recovery)"
if crash_fill "$db" 1500 1 1100; then
    MINISTORE_CRASH_POINT=page_write MINISTORE_CRASH_COUNT=8 \
        "$KVCLI" recover "$db" >/dev/null 2>&1
    ok  "crash during recovery: a later attempt succeeds" \
        "$KVCLI" verify "$db" 1100
    a="$("$KVCLI" dump "$db" | sha256sum)"
    "$KVCLI" recover "$db" >/dev/null
    b="$("$KVCLI" dump "$db" | sha256sum)"
    same "crash during recovery: result is stable" "$a" "$b"
else
    echo "FAIL crash during recovery: workload did not crash"
    FAILED=$((FAILED + 1))
fi

# ---- a crash during recovery, replaying on top of a checkpoint --------
# Here the replayed window changes pages that already existed before the
# crash, so a half finished recovery leaves some of those pages updated on
# disk and some not.  Finishing the job must not apply anything twice.
db="$(fresh_db idem_partial_replay)"
"$KVCLI" fill "$db" 1500 >/dev/null
if crash_fill "$db" 900 1501 600; then
    for attempt in 20 40; do
        MINISTORE_CRASH_POINT=page_write MINISTORE_CRASH_COUNT="$attempt" \
            "$KVCLI" recover "$db" >/dev/null 2>&1
    done
    ok  "partial replay: recovery completes cleanly" \
        "$KVCLI" verify "$db" 2100
    c="$("$KVCLI" dump "$db" | sha256sum)"
    "$KVCLI" recover "$db" >/dev/null
    "$KVCLI" recover "$db" >/dev/null
    d="$("$KVCLI" dump "$db" | sha256sum)"
    same "partial replay: result is stable" "$c" "$d"
    ok  "partial replay: still sound"       "$KVCLI" verify "$db" 2100
else
    echo "FAIL partial replay: workload did not crash"
    FAILED=$((FAILED + 1))
fi

# ---- recovery after a clean shutdown changes nothing -------------------
db="$(fresh_db idem_clean)"
"$KVCLI" fill "$db" 1000 >/dev/null
before="$("$KVCLI" dump "$db" | sha256sum)"
"$KVCLI" recover "$db" >/dev/null
"$KVCLI" recover "$db" >/dev/null
after="$("$KVCLI" dump "$db" | sha256sum)"
same "clean shutdown: recovery is a no-op" "$before" "$after"
ok   "clean shutdown: still sound"          "$KVCLI" verify "$db" 1000

finish_suite
