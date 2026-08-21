#!/usr/bin/env bash
# Recovery must be repeatable.  Opening a database that has already been
# recovered - or finishing a recovery that was itself interrupted - must
# leave exactly the same contents, however many times it happens.
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
setup_suite

# ---- recovering the same crashed database several times ---------------
db="$(fresh_db idem_repeat)"
if crash_fill_opts "$db" 1500 520 "--spread"; then
    ok  "repeated recovery: first pass is sound" \
        "$KVCLI" expect "$db" 1500 520 --spread
    first="$("$KVCLI" dump "$db" | sha256sum)"
    ok  "repeated recovery: second pass"  "$KVCLI" recover "$db"
    ok  "repeated recovery: still sound"  "$KVCLI" expect "$db" 1500 520 --spread
    second="$("$KVCLI" dump "$db" | sha256sum)"
    ok  "repeated recovery: third pass"   "$KVCLI" recover "$db"
    third="$("$KVCLI" dump "$db" | sha256sum)"
    same "repeated recovery: pass 2 matches pass 1" "$first" "$second"
    same "repeated recovery: pass 3 matches pass 1" "$first" "$third"
else
    echo "FAIL repeated recovery: workload did not crash"
    FAILED=$((FAILED + 1))
fi

# ---- a recovery that is itself interrupted ----------------------------
# The first attempt dies part way through writing the recovered pages, so
# the next attempt replays the same records over a data file that is
# partly updated.  Nothing may be applied twice.
db="$(fresh_db idem_interrupted)"
if crash_fill_opts "$db" 1500 660 "--spread"; then
    for attempt in 12 30; do
        MINISTORE_CRASH_POINT=page_write MINISTORE_CRASH_COUNT="$attempt" \
            "$KVCLI" recover "$db" >/dev/null 2>&1
    done
    ok  "interrupted recovery: a later attempt completes" \
        "$KVCLI" expect "$db" 1500 660 --spread
    a="$("$KVCLI" dump "$db" | sha256sum)"
    "$KVCLI" recover "$db" >/dev/null
    "$KVCLI" recover "$db" >/dev/null
    b="$("$KVCLI" dump "$db" | sha256sum)"
    same "interrupted recovery: result is stable" "$a" "$b"
    ok  "interrupted recovery: still sound" \
        "$KVCLI" expect "$db" 1500 660 --spread
else
    echo "FAIL interrupted recovery: workload did not crash"
    FAILED=$((FAILED + 1))
fi

# ---- an interrupted recovery on a checkpointed database ---------------
# Here the replayed window changes pages that already existed before the
# crash, so a half finished recovery leaves some of them updated on disk
# and some not.  Finishing the job must not apply anything twice.
db="$(fresh_db idem_partial_replay)"
MINISTORE_CRASH_POINT=commit MINISTORE_CRASH_COUNT=1600 \
    "$KVCLI" fill "$db" 2400 --spread --checkpoint-every 500 >/dev/null 2>&1
if [ "$?" -eq 90 ]; then
    for attempt in 10 25; do
        MINISTORE_CRASH_POINT=page_write MINISTORE_CRASH_COUNT="$attempt" \
            "$KVCLI" recover "$db" >/dev/null 2>&1
    done
    ok  "partial replay: recovery completes cleanly" \
        "$KVCLI" expect "$db" 2400 1600 --spread
    c="$("$KVCLI" dump "$db" | sha256sum)"
    "$KVCLI" recover "$db" >/dev/null
    "$KVCLI" recover "$db" >/dev/null
    d="$("$KVCLI" dump "$db" | sha256sum)"
    same "partial replay: result is stable" "$c" "$d"
    ok  "partial replay: still sound" \
        "$KVCLI" expect "$db" 2400 1600 --spread
else
    echo "FAIL partial replay: workload did not crash"
    FAILED=$((FAILED + 1))
fi

# ---- recovery after a clean shutdown changes nothing -------------------
db="$(fresh_db idem_clean)"
"$KVCLI" fill "$db" 2000 --spread >/dev/null
before="$("$KVCLI" dump "$db" | sha256sum)"
"$KVCLI" recover "$db" >/dev/null
"$KVCLI" recover "$db" >/dev/null
after="$("$KVCLI" dump "$db" | sha256sum)"
same "clean shutdown: recovery is a no-op" "$before" "$after"
ok   "clean shutdown: still sound"          "$KVCLI" verify "$db" 2000

finish_suite
