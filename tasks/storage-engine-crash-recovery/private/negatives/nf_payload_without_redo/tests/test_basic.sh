#!/usr/bin/env bash
# Normal operation: inserts, updates, deletes, reopen, clean shutdown.
set -u
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
setup_suite

db="$(fresh_db basic_small)"
ok  "small tree: fill"                 "$KVCLI" fill "$db" 10
ok  "small tree: verify"               "$KVCLI" verify "$db" 10
ok  "small tree: reopen is clean"      "$KVCLI" recover "$db"
ok  "small tree: still verifies"       "$KVCLI" verify "$db" 10

db="$(fresh_db basic_split)"
ok  "leaf splits: fill"                "$KVCLI" fill "$db" 200
ok  "leaf splits: verify"              "$KVCLI" verify "$db" 200

db="$(fresh_db basic_deep)"
ok  "deep tree: fill"                  "$KVCLI" fill "$db" 2000
ok  "deep tree: verify"                "$KVCLI" verify "$db" 2000
h="$("$KVCLI" stats "$db" | sed -n "s/.*height=\([0-9]*\).*/\1/p")"
same "deep tree: grew past two levels" "yes" "$([ "${h:-0}" -ge 3 ] && echo yes || echo no)"

db="$(fresh_db basic_update)"
ok  "updates: fill"                    "$KVCLI" fill "$db" 300
ok  "updates: rewrite a prefix"        "$KVCLI" update "$db" 120
ok  "updates: verify"                  "$KVCLI" verify "$db" 300

db="$(fresh_db basic_delete)"
ok  "deletes: fill"                    "$KVCLI" fill "$db" 120
ok  "deletes: remove a key"            "$KVCLI" del "$db" 42
fails "deletes: key is gone"           "$KVCLI" get "$db" 42
ok  "deletes: tree still sound"        "$KVCLI" verify "$db"
ok  "deletes: reinsert"                "$KVCLI" fill "$db" 1 --start 42
ok  "deletes: verify"                  "$KVCLI" verify "$db" 120

db="$(fresh_db basic_spread)"
ok  "scattered inserts: fill"          "$KVCLI" fill "$db" 2500 --spread
ok  "scattered inserts: verify"        "$KVCLI" verify "$db" 2500

db="$(fresh_db basic_ckpt)"
ok  "checkpoints: fill with checkpoints" \
    "$KVCLI" fill "$db" 500 --checkpoint-every 50
ok  "checkpoints: verify"              "$KVCLI" verify "$db" 500

db="$(fresh_db basic_reopen)"
ok  "reopen: first batch"              "$KVCLI" fill "$db" 400
ok  "reopen: second batch"             "$KVCLI" fill "$db" 400 --start 401
ok  "reopen: third batch"              "$KVCLI" fill "$db" 400 --start 801
ok  "reopen: verify all"               "$KVCLI" verify "$db" 1200
before="$("$KVCLI" dump "$db" | sha256sum)"
ok  "reopen: clean restart"            "$KVCLI" recover "$db"
after="$("$KVCLI" dump "$db" | sha256sum)"
same "reopen: contents unchanged"      "$before" "$after"

finish_suite
