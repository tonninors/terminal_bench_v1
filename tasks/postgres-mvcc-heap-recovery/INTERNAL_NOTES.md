# Internal notes

Author-side only. Nothing here is shown to the solver.

## How the case is built

`build/generate_case.py` starts a throwaway `postgres:16` container
(`autovacuum=off`, `fsync=off`, `full_page_writes=off`), installs
`python3-psycopg2` in it, and runs `build/pg_fixture.py` as the `postgres` OS
user against the live server over the unix socket. Nothing is downloaded into
the task; the container is removed afterwards.

The table:

```sql
CREATE TABLE public.account_ledger (
    account_id     integer               NOT NULL,
    region_code    character varying(12) NOT NULL,
    balance_cents  integer               NOT NULL,
    is_active      boolean               NOT NULL,
    risk_tier      smallint,
    opened_on      date                  NOT NULL,
    owner_note     text,
    CONSTRAINT account_ledger_pkey PRIMARY KEY (account_id)
) WITH (fillfactor = 60, autovacuum_enabled = false,
        toast.autovacuum_enabled = false);
CREATE INDEX account_ledger_region_idx ON public.account_ledger (region_code);
```

The column order is chosen to exercise alignment: a short varlena
(`region_code`) sits between two `integer`s, so the parser has to re-align to 4
after a tightly packed 1-byte-header datum; `is_active`/`risk_tier`/`opened_on`
step through 1-, 2- and 4-byte alignment in a row; and the nullable `text`
(`owner_note`) is last, so a NULL there simply ends the tuple early. `fillfactor
= 60` leaves room in each block for HOT updates. The secondary index on
`region_code` is what makes updates that change that column *non*-HOT, so the
fixture contains both kinds.

## Transaction history (v2)

The generator runs four phases. What separates v2 from v1 is a **horizon
holder**: a read-only `REPEATABLE READ` session opened at the end of phase A and
kept open until capture. It is never assigned an xid, so it appears in no
snapshot and in no commit log, but it pins the vacuum horizon - and from that
point PostgreSQL prunes nothing. Every physical version created afterwards
survives into the captured file.

| # | phase | effect |
| --- | --- | --- |
| A1 | base insert, committed | every base id, in id order, so allocation order is physical order |
| A2 | 22 rounds of HOT churn on `A_churn` / `A_churn_long`, with a scan between rounds | pruning runs while the horizon is still current, leaving the `LP_REDIRECT` roots |
| A3 | a committed non-HOT update and a committed delete, then four scans | dead **root** line pointers, which pruning retires as `LP_DEAD` (not `LP_UNUSED`, because index entries still point at them) |
| — | **the horizon holder opens** | nothing is pruned from here on |
| B1 | row locks: `FOR UPDATE`, `FOR NO KEY UPDATE`, `FOR SHARE`, `FOR KEY SHARE`, one aborted `FOR UPDATE`, and a committed update followed by a committed lock of the new version | 53 lock-only `xmax` tuples, **none** stamped `HEAP_XMAX_INVALID` |
| B2 | committed updates, multi-round updates, a committed delete | ordinary visible history |
| B3 | aborted insert, aborted update, aborted delete | dead versions that keep the greatest `xmin` on their keys |
| B4 | abort-then-commit, and commit-abort-lock | keys where three mechanisms interact at once |
| C | nine writers opened **interleaved** with five transactions that commit between them | a sparse `snapshot_xip` inside `[xmin, xmax)` |
| — | **the target snapshot is taken** by a read-only `REPEATABLE READ` observer | `781:795:781,783,785,787,788,790,791,792,793` |
| D | after the snapshot: committed update, insert, delete, three more HOT rounds, a committed lock, an aborted update and an aborted delete | committed-but-invisible work, and chains whose visible member is in the middle |
| — | writers 1-4 commit, writer 5 aborts, writers 6-9 stay open | four `committed` xids inside `xip`, one `aborted` inside `xip`, four `in_progress` |
| — | `CHECKPOINT`, then the relation's main fork is copied byte for byte | `heap_pages.bin` |

### Why the horizon holder is the whole trick

Three things depend on it, and all three were weak in v1:

1. **Lock-only `xmax` stays load-bearing.** `HeapTupleSatisfiesVacuum` stamps
   `HEAP_XMAX_INVALID` on a lock-only tuple as soon as pruning touches its page
   and the locker is no longer running. In v1 that happened to all three
   lock-only tuples, so a solver could honour `HEAP_XMAX_INVALID` alone and never
   need `HEAP_XMAX_LOCK_ONLY`. With the horizon pinned, no prune runs, and all 53
   lock-only tuples keep a bare lock bit. 33 of them name an `xmax` that both
   committed and is visible to the snapshot, so the infomask is now mandatory.
2. **HOT chains survive.** 40 keys keep three or more versions, the deepest chain
   is seven, and for 12 keys the visible version is strictly mid-chain.
3. **Aborted versions survive**, so the greatest-`xmin` tuple is an aborted one
   for 47 keys.

The locks deliberately run *after* the holder opens but *before* any concurrent
writer, so their xids land below `snapshot_xmin` - plainly visible, with no
snapshot escape hatch for a solver that skips the infomask.

### Why the writers are interleaved

`pg_current_snapshot()` sets `xmax = latestCompletedXid + 1`, so writers opened
back-to-back all sit at or above `xmax` and `xip` comes out empty. Committing a
transaction between each pair of writers lifts `xmax` above them and, more
importantly, leaves **committed xids interleaved with the `xip` entries**:
782, 784, 786, 789 and 794 are committed and visible, while 781, 783, 785 and 787
are in `xip` and are not. That kills both shortcuts at once - ignoring `xip`, and
collapsing it into "anything at or above `snapshot_xmin` is still running", which
in v1 was wrong on only 9 keys.

## Ordering constraints (do not reshuffle casually)

* **HOT pruning must happen before the horizon holder opens.** Pruning is bounded
  by the oldest running snapshot; once the holder is open nothing is reclaimed and
  there would be no `LP_REDIRECT` or `LP_DEAD` at all.
* **Conversely, everything that must survive has to happen after it.** Moving the
  lock phase back before the holder was tried first and cost 27 of the 33
  decisive lock-only tuples: the `A_upd_then_lock` update set `pd_prune_xid` on
  the shared page, and the next index scan pruned it and stamped
  `HEAP_XMAX_INVALID` over every completed locker on that block.
* **The nine open writers must not share rows with any later phase.** They hold
  row locks; a post-snapshot transaction touching the same ids would block
  forever, and a second updater or locker on an already-locked row is exactly how
  a MultiXact `xmax` appears. Every id range is disjoint by construction.
* **The observer and the horizon holder must stay read-only.** `pg_fixture.py`
  asserts `pg_current_xact_id_if_assigned()` is NULL for both, so neither appears
  in the snapshot or in `tx_status.csv`.
* **The file is copied before the observer runs its final `SELECT`,** so the
  reference query cannot add hint bits to the bytes the solver receives.

## Deliberately excluded tuple cases

These were flagged during proposal review. They are **not implemented**, so the
fixture is built to exclude them and `pg_fixture.py` aborts generation if any
appears. `build/fixture_test.py` re-asserts all four from the solver-facing bytes.

| case | why it is excluded | how it is kept out | assertion |
| --- | --- | --- | --- |
| **Combo CIDs** (`HEAP_COMBOCID`, `0x0020`) | resolving `t_cid` into a `cmin`/`cmax` pair needs the writing backend's in-memory combo array, which no on-disk artefact can supply | no transaction both creates and removes the same tuple; and an external snapshot never consults a command id in the first place | `test_no_combo_command_ids` |
| **Frozen tuples** (`t_infomask & 0x0300 == 0x0300`, or `xmin` 1/2) | correct handling means treating `xmin` as older than every snapshot and bypassing the commit log | no `VACUUM`/`VACUUM FREEZE` runs, `autovacuum` is off, and the cluster is fresh so no xid is near the freeze horizon | `test_no_frozen_tuples` |
| **MultiXact `xmax`** (`HEAP_XMAX_IS_MULTI`, `0x1000`) | visibility would depend on `pg_multixact` membership, which is not part of the input | no foreign keys (so no share locks), a single locker for the one `FOR UPDATE`, and no second writer on a locked row | `test_no_multixact_xmax` |
| **Out-of-line TOAST** (`HEAP_HASEXTERNAL`, `0x0004`; 1-byte header `0x01`) | the TOAST relation is not shipped | every text value is far below the 2 KiB threshold; the generator also asserts the TOAST relation is empty | `test_no_out_of_line_toast_datum` |

The oracle nevertheless *handles* frozen tuples correctly and raises a clear
error on MultiXact, combo CIDs and external datums, rather than silently
producing a wrong answer.

The prompt does not enumerate these exclusions. It does state that all values are
inline and that no TOAST data is needed, because that is information the solver
genuinely needs in order to know the task is well-posed. The other three cases
simply do not occur, so mentioning them would only hint at the solution.

## Generation-time assertions

`pg_fixture.py` refuses to write a fixture that would be too easy. It aborts if
any excluded tuple case appears, if there is no `LP_REDIRECT` or `LP_DEAD`, if
fewer than 15 lock-only tuples require the infomask, if fewer than 40 heap-only
tuples survive, if fewer than three committed transactions are in `xip`, if fewer
than three committed transactions sit inside the xid range but outside `xip`, if
no transaction in `xip` aborted, or if any writer is missing from `xip`.
`build/fixture_test.py` re-derives all of it from the solver-facing bytes alone,
and `build/measure_negatives.py` additionally asserts that no wrong strategy is
within 10 primary keys of correct.

## Reproducibility

PostgreSQL assigns transaction ids and page LSNs itself. Forcing byte-for-byte
equality would mean editing genuine page structures, which would defeat the point
of using a real server, so exact input reproducibility is not a requirement here.

In practice the procedure is stronger than required: two consecutive
`build/generate_case.py` runs against fresh containers produced **byte-identical**
`heap_pages.bin`, `tx_status.csv`, `table_schema.json`, `golden.csv` and
`expected_state.json` (verified during authoring). A third, earlier run with the
container started by hand produced different page LSNs but the **same logical
expected state** — identical `golden.csv` and identical canonical digest
`31137c1baca1ff41b2f194e807f3bb1462d95d71aefb3ece8979605a674e8a11`.

What *is* guaranteed:

* the generation procedure is scripted end to end and rerunnable;
* the logical expected state is deterministic;
* the oracle is deterministic (asserted by
  `harness_test.py::test_oracle_is_deterministic`);
* the verifier's verdict is deterministic (asserted twice, in the verifier itself
  and in the harness).

If the fixture is regenerated, `build/generate_case.py` rebuilds
`tests/expected_state.json`, `solution.sh` and the ZIP in the same run, so the
package stays self-consistent. Run `build/run_all_validation.sh` afterwards.

## Things a reviewer might ask

**Is `pageinspect` needed to solve it?** No. It is used only during generation,
to cross-check the parser against the server's own reading of the same bytes
(`build/internal/page_items.json`). The task container has no PostgreSQL at all.

**Could a solver just start PostgreSQL and attach the file?** No — there is no
`pg_xact`, no `pg_control`, no catalog, and the container ships no server. Even
with one, PostgreSQL would have no commit log for xids 735–779, and no way to
impose the target snapshot.

**Does `t_ctid` chain following matter?** Not for the answer: every live version
occupies its own `LP_NORMAL` slot, so visiting all of them is sufficient. What
matters is *not* treating `LP_REDIRECT` as a tuple. The oracle records `t_ctid`
but does not need to walk it.

**Why is `owner_note` allowed to contain a comma?** `priority, escalated` is an
ordinary value, and RFC 4180 quoting is part of the stated output contract. It
makes a hand-rolled `','.join(...)` writer fail, which is a real CSV bug, not a
trick.

**Row order:** genuinely free. The verifier hashes rows keyed by primary key and
separately asserts that reversing and re-sorting the records does not change the
verdict.
