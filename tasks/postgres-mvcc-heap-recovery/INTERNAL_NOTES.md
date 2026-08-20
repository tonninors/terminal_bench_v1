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
) WITH (fillfactor = 70, autovacuum_enabled = false,
        toast.autovacuum_enabled = false);
CREATE INDEX account_ledger_region_idx ON public.account_ledger (region_code);
```

The column order is chosen to exercise alignment: a short varlena
(`region_code`) sits between two `integer`s, so the parser has to re-align to 4
after a tightly packed 1-byte-header datum; `is_active`/`risk_tier`/`opened_on`
step through 1-, 2- and 4-byte alignment in a row; and the nullable `text`
(`owner_note`) is last, so a NULL there simply ends the tuple early. `fillfactor
= 70` leaves room in each block for HOT updates. The secondary index on
`region_code` is what makes updates that change that column *non*-HOT, so the
fixture contains both kinds.

## Transaction history

Phases, in order (xids from the current fixture in brackets):

| # | phase | effect |
| --- | --- | --- |
| 1 | base insert, committed [735] | ids 1–65 plus filler ids 1001–1160 |
| 2 | 24 rounds of HOT churn on ids 1–6, each committed, with a scan between rounds to provoke pruning [736–759] | produces the `LP_REDIRECT` roots and `LP_DEAD` slots |
| 2b | final HOT update, committed [760] | the versions visible for ids 1–6 |
| 3 | committed update changing `region_code` (non-HOT) on ids 7–12 and filler 1091–1120 [761] | second live version on later blocks |
| 4 | committed delete of ids 13–17 and filler 1121–1130 [762] | invisible rows |
| 5 | three committed update rounds on ids 29–34 [763–765] | several versions of one key |
| 6 | **aborted** insert of ids 501–504 [766] | dead tuples with an aborted `xmin` |
| 7 | **aborted** update of ids 18–23 and filler 1041–1070 [767] | old version stays visible, new version dead |
| 8 | **aborted** delete of ids 24–28 [768] | `xmax` set by an aborted transaction |
| 9 | committed `SELECT ... FOR UPDATE` on ids 35–37 [769] | `HEAP_XMAX_LOCK_ONLY` tuples |
| 10 | three writers opened and left running [770, 771, 772] | uncommitted inserts, updates and deletes on the pages |
| 10b | a committed update while those three are open [773] | forces `snapshot_xmax` above the running xids so `snapshot_xip` is non-empty |
| 11 | **the target snapshot** is taken by a `REPEATABLE READ` session | `770:774:770,771,772` |
| 12 | after the snapshot: committed update [774], committed insert [775], committed delete [776], committed HOT update [777], aborted update [778], aborted delete [779] | all invisible-but-committed, or visible-despite-deleted |
| 12b | writer 772 commits | now `committed` in `tx_status.csv` while still listed in `snapshot_xip` |
| 13 | `CHECKPOINT`, then the relation's main fork is copied byte for byte | `heap_pages.bin` |

Writers 770 and 771 are never committed, so they are reported as `in_progress`.

### Why phase 10b exists

`pg_current_snapshot()` sets `xmax = latestCompletedXid + 1`. With only open
writers and no completed transaction after them, every running xid is already at
or above `xmax` and `xip` comes out empty — the first attempt at this fixture hit
exactly that. Committing one transaction while 770–772 are still open lifts
`xmax` to 774 and puts all three into `xip`, which is what makes the
`snapshot_xip` reasoning load-bearing.

### Why the snapshot is taken before the last phases

The snapshot must disagree with the commit log in both directions:

* transactions **at or above `snapshot_xmax`** (774–777) are `committed` in
  `tx_status.csv` and invisible;
* transaction **772 is inside `snapshot_xip`** and also `committed` — the case
  that punishes reading the commit log and ignoring the snapshot's in-progress
  list.

## Ordering constraints (do not reshuffle casually)

* **HOT pruning must happen before any long-lived transaction opens.** Pruning is
  bounded by the oldest running snapshot; with the observer or the three writers
  open, nothing would be reclaimed and there would be no `LP_REDIRECT`.
* **The three open writers must not share rows with any later phase.** They hold
  row locks; a post-snapshot transaction touching the same ids would block
  forever, and a second updater/locker on an already-locked row is exactly how a
  MultiXact `xmax` appears. Every id range is disjoint by construction.
* **The observer transaction must stay read-only.** `pg_fixture.py` asserts that
  `pg_current_xact_id_if_assigned()` is NULL for it, so the observer never
  appears in its own snapshot.
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
