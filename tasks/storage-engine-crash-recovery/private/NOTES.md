# Internal notes — ministore transaction-atomicity task (NOT solver facing)

Everything in `private/` stays out of the solver bundle and out of the
task image. `build/make_zip.py` only ever walks `repo/`, and additionally
refuses to package any path whose name contains `private`, `fixed`,
`reference`, `negative`, `solution`, `answer`, `oracle` or `golden`.

## The engine as shipped

`repo/` is a working WAL-backed B+tree storage engine with an explicit
multi-operation transaction API:

    kv_begin(s); kv_put(...); kv_delete(...); kv_commit(s) / kv_abort(s)

`kv_put` and `kv_delete` called outside `kv_begin` run in a transaction of
their own, so the single-write case is the one-operation case of the same
rule.

Two budgets are fixed and documented, and neither scales with the size of
a transaction:

* the buffer pool is 32 frames (`src/pager.c`), stated in `README.md` and
  checked by a public test that reads `kvcli stats`;
* the log is checkpointed once it passes `MS_LOG_LIMIT` (32 KiB), and the
  check runs after every operation, not only at commit.

Measured: a transaction of 150 operations touches ~150 distinct leaves
(far past 32 frames) and drives the log to 32,880 bytes, past the 32 KiB
budget. So a large transaction *must* have pages written back before it
commits, and *must* trigger a checkpoint part way through. That is the
architecture, not a trick, and it is what the README describes.

## The planted defect

`src/kvstore.c`. `txn_snapshot()` keeps the page as it was before the
running transaction first changed it — **in memory only**:

```c
    s->undo[s->undo_n].page = pg->id;
    memcpy(s->undo[s->undo_n].img, pg->buf, PAGE_SIZE);
```

`kv_abort()` walks that array newest-first and puts the pages back, so
rolling a transaction back while the process is alive works correctly.
Nothing about it survives the process. The engine steals — the pool
evicts an unfinished transaction's pages, and the log budget forces a
checkpoint that flushes them on purpose and then recycles the records —
but there is no durable undo information, and recovery has no undo pass.
A crash therefore leaves part of an uncommitted transaction on disk with
nothing able to take it back off.

Why the shape is right:

* every clean path is correct: normal writes, aborts, clean shutdown,
  and redo of committed work all behave. The shipped tree is 58/62 on
  its own suite, and the four failures are exactly the crash cases.
* the damage is **logical, not structural**, in the principal cases. A
  transaction that rewrites and deletes existing keys never splits a
  page, so the recovered tree passes `kv_verify` and the only thing wrong
  is that some of an uncommitted transaction's operations are there and
  the rest are not. Measured over a 47-point crash sweep: rewrite-only
  and rewrite+delete workloads give 0 structurally invalid outcomes.
* transactions that *insert* do split pages, and an uncommitted split
  whose halves reach disk unevenly does leave the tree structurally
  invalid. Those cases are kept deliberately and in the minority: they
  are what makes "roll the data back but not the structure" fail.

Nothing in the shipped tree names the cause. The `undo` array is the
engine's own honest design for `kv_abort`; noticing that it is never
written anywhere is the start of the work, not the end of it.

## The reference repair

`private/reference_fix.patch`, built by `build/make_reference.py`. Five
files, +226/-30 lines. It is not a local edit; it is four coordinated
changes:

1. **a durable before-image.** `txn_snapshot()` appends a `WR_UNDO`
   record carrying the whole page, and raises the page LSN so the
   pager's existing write-ahead rule holds the page back until that
   record is on disk.
2. **an undo pass in recovery.** After redo, `recover_undo()` collects
   the `WR_UNDO` records of every transaction that has undo records but
   neither committed nor finished rolling back, and applies them newest
   first.
3. **an end-of-rollback record.** `kv_abort()` flushes the restored
   pages, syncs, and only then writes `WR_END`. Without it, recovery
   would undo an already-aborted transaction a second time, on top of
   committed work that ran after it.
4. **a recycling horizon.** A checkpoint may still flush an unfinished
   transaction's pages, but may no longer truncate the log while a
   transaction is live — the records it would drop are the only
   description of what those pages held.

The meta page is restored field-wise, not wholesale: `root`, `height`
and `num_pages` belong to the transaction and go back; `checkpoint_lsn`
and `next_lsn` are facts about the log and must not.

Grading is behavioural. Undo records in the log, a rollback journal in a
separate file, and ARIES-style CLRs are all acceptable; the private
verifier only ever looks at what the database contains afterwards.

## What the private verifier varies

`private/verify.sh`, 42 cases. It varies only what the prompt, README and
FORMAT.md already say varies: which of the four documented fault points
the crash lands on, how far in, transaction size relative to the pool and
the log budget, which kinds of operation the transaction contains,
whether some transactions are aborted outright, and whether recovery is
repeated or itself interrupted and re-run. Crash counts are calibrated
against the shipped engine, which performs the fewest page writes of any
build being graded, so every case really does stop where it says.
