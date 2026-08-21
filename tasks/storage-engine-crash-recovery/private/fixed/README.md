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
    build/kvcli fill    data.db 5000 --spread    the same keys, scattered
    build/kvcli fill    data.db 100 --start 6000
    build/kvcli verify  data.db 5000            structural check + sweep
    build/kvcli expect  data.db 5000 1200        exactly the committed keys
    build/kvcli get     data.db 42
    build/kvcli del     data.db 42
    build/kvcli batch   data.db --txns 20 --size 200
    build/kvcli rewrite data.db --txns 20 --size 200 --abort-every 4
    build/kvcli stats   data.db
    build/kvcli dump    data.db                 every pair, in key order
    build/kvcli recover data.db                 open (running recovery), close
    build/kvcli waldump data.db                 decode the log

`--checkpoint-every N` makes `fill` checkpoint as it goes.

`batch` runs multi-operation transactions that insert new keys; `rewrite`
runs multi-operation transactions that rewrite keys already in the
database.  `--abort-every A` aborts every Ath one.  The matching
`expect-batch` and `expect-rewrite` commands check the all-or-nothing
rule: they report any transaction that is only partly in the database.

## Transactions

    kv_begin(s);
    kv_put(s, k1, v1, n1);
    kv_delete(s, k2);
    kv_put(s, k3, v3, n3);
    kv_commit(s);            /* or kv_abort(s) */

A transaction may contain any number of inserts, updates and deletes,
over any number of keys, touching any number of pages.  The rule it has
to obey is all or nothing:

* if `kv_commit` returned, every one of its operations is in the
  database and stays there;
* if it was aborted with `kv_abort`, or if the process died before the
  commit, none of its operations is in the database - not some of them,
  none;

and that holds after a crash and recovery just as it holds during normal
running.  `kv_put` and `kv_delete` called outside `kv_begin` run inside a
transaction of their own, so a single write is just the one-operation
case of the same rule.

## Durability model

Every change a transaction makes is described by a log record before the
page it changes may be written: a `BEGIN` record, one record per page
changed, and a `COMMIT` record that is flushed to disk before
`kv_commit` returns.  Data pages are written back lazily.

Two budgets are fixed and neither scales with the size of a transaction:

* **the buffer pool** holds 32 pages.  That is a fixed property of the
  engine, not a tuning knob: ministore is built for machines whose
  memory does not grow with the database, and it is expected to run
  databases and transactions orders of magnitude larger than the pool.
  A transaction that touches more pages than 32 will have pages written
  back underneath it, as they are evicted, in whatever order eviction
  happens to pick - long before it commits.  `kvcli stats` reports the
  pool size and `tests/test_basic.sh` checks it, so growing the pool
  until a transaction fits in memory is not an available answer.
* **the log** may not pass `MS_LOG_LIMIT` (32 KiB).  The budget is
  checked after every operation, not only at commit, because a single
  transaction can write far more log than that on its own.  When the
  budget is passed the engine checkpoints: it flushes every dirty page,
  syncs the data file, records how far the log has been absorbed, and
  recycles the log.

So a large transaction is *expected* to have some of its pages on disk,
and some of its early log records recycled, well before anyone knows
whether it will commit.  Holding everything in memory until commit is
not available to this engine at these budgets.

Recovery replays committed records that are newer than the last
checkpoint, skipping any page whose stored LSN shows the change already
reached the data file.  Recovery has to be repeatable: running it twice
in a row must leave exactly the same database, and being interrupted
part way through and re-run must too.

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

Normal operation and clean shutdown are stable and in use, including
abort: rolling a transaction back while the process is alive behaves.
Crash recovery is under active investigation.  Reports from the field
say that after a power cut a database can come back holding part of a
transaction that never committed - the tree checks out, the counts look
plausible, but some of the writes from an interrupted transaction are
there and the rest are not.  Reproducing that is what the crash cases in
the suite are for.

## Licence

All source, tests and documentation in this repository were written for
this project from scratch.
