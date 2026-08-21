# ministore

A small embedded key/value storage engine: a B+tree over fixed size pages,
made durable by a write-ahead log with redo recovery.  It is a single
writer engine with no background threads and no external dependencies -
the whole thing is C11 plus the POSIX file API.

    keys    unsigned 64 bit integers
    values  up to 52 bytes, stored inline in the leaf
    pages   1024 bytes, up to 15 pairs per leaf and 63 children per
            internal node

## Layout

    include/kvstore.h   public API (frozen)
    src/pager.c         page cache and data file I/O
    src/wal.c           the append only log
    src/node.c          single page operations (search, insert, split)
    src/btree.c         tree maintenance and the records it logs
    src/recover.c       redo recovery
    src/kvstore.c       transactions, checkpoints, self-check
    tools/kvcli.c       command line driver
    tests/              the test suite
    docs/FORMAT.md      the on-disk format

## Building

    make                    builds build/libministore.a and build/kvcli
    make test               builds, then runs tests/run_all.sh
    make clean

Only a C compiler and make are needed.  Nothing downloads anything and
nothing talks to the network.

## Using the command line driver

    build/kvcli fill    data.db 5000            insert keys 1..5000
    build/kvcli fill    data.db 100 --start 6000
    build/kvcli verify  data.db 5000            structural check + sweep
    build/kvcli get     data.db 42
    build/kvcli del     data.db 42
    build/kvcli stats   data.db
    build/kvcli dump    data.db                 every pair, in key order
    build/kvcli recover data.db                 open (running recovery), close
    build/kvcli waldump data.db                 decode the log

`--checkpoint-every N` makes `fill` checkpoint as it goes.

## Durability model

Each `kv_put` and `kv_delete` runs as one logged transaction: a `BEGIN`
record, one record per page the operation changes, then a `COMMIT` record
that is flushed to disk before the call returns.  Data pages are written
back lazily; a page is never written before the log record describing its
newest change is durable.

A checkpoint flushes every dirty page, syncs the data file, and writes a
checkpoint record.  Recovery replays committed records that are newer than
the last checkpoint, skipping any page whose stored LSN shows the change
already reached the data file.  Recovery therefore has to be repeatable:
running it twice in a row must leave exactly the same database.

## Fault injection

The tests simulate power loss with two environment variables:

    MINISTORE_CRASH_POINT   commit | page_write | meta_write | checkpoint
    MINISTORE_CRASH_COUNT   die the Nth time that point is reached

The process exits immediately with status 90, without flushing anything,
which is what an unplanned power cut looks like to the next process that
opens the database.

    MINISTORE_CRASH_POINT=commit MINISTORE_CRASH_COUNT=500 \
        build/kvcli fill data.db 2000
    build/kvcli verify data.db 500

## Tests

    tests/test_basic.sh           inserts, updates, deletes, clean restarts
    tests/test_crash_recovery.sh  power loss at scripted points
    tests/test_idempotence.sh     recovery repeated, and interrupted

`tests/run_all.sh` runs all three and exits non-zero if anything fails.
The suite is deterministic: the same commands always produce the same
verdict.

## Status

Normal operation and clean shutdown are stable and in use.  Crash
recovery is under active investigation: some of the crash and idempotence
cases in the suite do not pass yet.

## Licence

All source, tests and documentation in this repository were written for
this project from scratch.
