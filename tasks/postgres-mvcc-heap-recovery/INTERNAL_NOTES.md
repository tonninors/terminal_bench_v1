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

## Transaction history (v4)

The generator runs four phases. What separates v2 from v1 is a **horizon
holder**: a read-only `REPEATABLE READ` session opened at the end of phase A and
kept open until capture. It is never assigned an xid, so it appears in no
snapshot and in no commit log, but it pins the vacuum horizon - and from that
point PostgreSQL prunes nothing. Every physical version created afterwards
survives into the captured file.

| # | phase | effect |
| --- | --- | --- |
| A1 | base insert, committed | every base id, in id order, so allocation order is physical order |
| A1b | `VACUUM (FREEZE)` | every base tuple gets `HEAP_XMIN_FROZEN` (both hint bits) with its raw xmin preserved; every row a later phase touches becomes a frozen-xmin-plus-live-xmax case |
| A2 | 22 rounds of HOT churn on `A_churn` / `A_churn_long`, with a scan between rounds | pruning runs while the horizon is still current, leaving the `LP_REDIRECT` roots |
| A2b | a plain `VACUUM` after the churn | reclaims the dead churn versions while the horizon is still current; the redirects survive |
| A3 | a committed non-HOT update and a committed delete, then four scans | dead **root** line pointers, which pruning retires as `LP_DEAD` (not `LP_UNUSED`, because index entries still point at them) |
| — | **the horizon holder opens** | nothing is pruned from here on |
| B1 | row locks: `FOR UPDATE`, `FOR NO KEY UPDATE`, `FOR SHARE`, `FOR KEY SHARE`, one aborted `FOR UPDATE`, and a committed update followed by a committed lock of the new version | 53 lock-only `xmax` tuples, **none** stamped `HEAP_XMAX_INVALID` |
| B2 | committed updates, multi-round updates, a committed delete | ordinary visible history |
| B3 | aborted insert, aborted update, aborted delete | dead versions that keep the greatest `xmin` on their keys |
| B4 | abort-then-commit, and commit-abort-lock | keys where three mechanisms interact at once |
| B5 | a committed transaction with four savepoints: two released, two rolled back | an ABORTED child under a COMMITTED parent, in both the update and the delete direction |
| B6 | frozen roots explicitly deleted, almost-deleted and locked | frozen xmin + committed / aborted / lock-only xmax |
| B7 | **the MultiXact groups** (after two burn multis on the uncaptured probe table) | locker-only multis in three mode mixes, one with a member that never finishes; locker+updater multis whose updater commits, aborts, stays open, or ran inside a released savepoint |
| C | eleven writers opened **interleaved** with six transactions that commit between them | a sparse `snapshot_xip` inside `[xmin, xmax)` |
| C1 | one of those writers holds **three nested savepoints** and commits after the snapshot | subxids that read COMMITTED and match no xip entry |
| C2 | a locker commits pre-snapshot while writer w12 updates the same rows | a multi whose **updater is in `snapshot_xip`** |
| C3 | a locker + writer w13 updating from **two nested savepoints** | multis whose updater is a **subxid of an xip writer** - the deepest chain in the fixture - including on the churned cascade keys |
| — | **the target snapshot is taken** by a read-only `REPEATABLE READ` observer | `789:811:789,791,793,795,796,802,804,805,806,807,808` |
| D | after the snapshot: committed update, insert, delete, three more HOT rounds, a committed lock, an aborted update and an aborted delete | committed-but-invisible work, and chains whose visible member is in the middle |
| D2 | a whole locker+updater multi after the snapshot | both members commit at/above `snapshot_xmax`; the old version stays visible |
| — | writers 1-4, 10, 12, 13 commit; writer 5 aborts; 6-9, 11 and the two multi holdouts stay open | committed, aborted and in-progress xids inside `xip` |
| — | **the trailing sentinel multi** on the probe table | bounds the last referenced multi's member list |
| — | `CHECKPOINT`, then the relation's main fork **and the `pg_xact` / `pg_subtrans` directories** are copied byte for byte | `heap_pages.bin`, `pg_xact/0000`, `pg_subtrans/0000` |

### The MultiXact mechanics, and their two sharp edges

A multi is created by ordinary SQL the moment a second transaction locks a row
whose xmax already names a live locker, or an updater modifies a row a
compatible locker still holds (`FOR KEY SHARE` + a non-key `UPDATE` ->
`{keysh, nokeyupd}`). All modes used here are mutually compatible, so the
strictly sequenced sessions never block and generation stays deterministic;
mxids allocate 1, 2, 3... exactly like xids.

Two on-disk edges are handled explicitly and asserted at generation time:

1. **Member offset 0 is reserved.** `offsets[first_multi] = 1`, so a zero entry
   can mean "never written". A solver that treats the array as plainly 0-based
   reads every member window shifted by one.
2. **`offsets[M+1]` bounds multi M's member list**, and for the newest multi on
   disk that entry exists only in `pg_control` (not shipped). The generator
   therefore creates one trailing sentinel multi - on the *uncaptured* probe
   table, so the sentinel itself never reaches the pages - and asserts
   `offsets[max_used + 1] != 0`.

Two further generation details matter:

* **The first two mxids are burned** on the probe table. Read as plain xids,
  mxid 1 is `BootstrapTransactionId` and mxid 2 is `FrozenTransactionId`, which
  PostgreSQL special-cases rather than recording in clog - so a solver that
  misreads them would accidentally survive. Burning them makes every on-page
  mxid >= 3, and the generator asserts each one decodes as COMMITTED when
  misread, keeping the IS_MULTI trap armed on every multi tuple.
* **The generator cross-checks every multi against the server**: the raw
  offsets/members decode must equal `pg_get_multixact_members()` member for
  member, flag for flag, before anything is written out.

### Why freezing runs before the horizon holder

`VACUUM (FREEZE)` prunes, so it must run while pruning is still allowed - and
it must run *before* the churn, so the churned keys' chains grow on top of
already-frozen roots. Everything modified after the horizon holder opens keeps
both its frozen root and its new xmax on the page. Freezing preserves the raw
xmin (verified: no tuple carries xid 1 or 2), so `pg_xact` remains sufficient
for every xid - the frozen bits are a *correctness* trap (bit ordering, and
"frozen means visible" skipping the xmax), not a data dependency.

### Why the horizon holder is the whole trick

Three things depend on it, and all three were weak in v1:

1. **Lock-only `xmax` stays load-bearing.** `HeapTupleSatisfiesVacuum` stamps
   `HEAP_XMAX_INVALID` on a lock-only tuple as soon as pruning touches its page
   and the locker is no longer running. In v1 that happened to all three
   lock-only tuples, so a solver could honour `HEAP_XMAX_INVALID` alone and never
   need `HEAP_XMAX_LOCK_ONLY`. With the horizon pinned, no prune runs, and all 53
   lock-only tuples keep a bare lock bit. 33 of them name an `xmax` that both
   committed and is visible to the snapshot, so the infomask is now mandatory.
2. **HOT chains survive.** Keys keep three or more versions, chains run seven
   deep, and for a dozen keys the visible version is strictly mid-chain.
3. **Aborted versions survive**, so the greatest-`xmin` tuple is an aborted one
   for 47 keys.

The locks deliberately run *after* the holder opens but *before* any concurrent
writer, so their xids land below `snapshot_xmin` - plainly visible, with no
snapshot escape hatch for a solver that skips the infomask.

### Why subtransactions are the v3 lever

`pg_current_snapshot()` builds its xip list from the snapshot's `xip` array,
which holds **top-level** xids. It does not export `subxip`. So when a
transaction writes inside a `SAVEPOINT`, the tuple carries the subtransaction's
own xid, and that xid appears in no `snapshot_xip` entry. Once the parent
commits, `pg_xact` reports the subxid as `COMMITTED`.

A solver reading only the commit log therefore sees "committed, not listed as
running, inside the xid range" and concludes the work is visible. It is not: the
topmost parent was still in flight when the snapshot was taken. Resolving that
needs `pg_subtrans`, and nothing else in the inputs can substitute for it.

The fixture pins this down in three directions so no blanket rule works:

* **w10** holds three *nested* savepoints across the snapshot and commits
  afterwards. Its subxids must be resolved to a parent that is in `xip` -
  %d tuple stamps depend on it, and the chain is %d hops deep, so a single-hop
  lookup is not enough.
* **the gap savepoint transaction** commits *between* the writers. Its subxid is
  equally a subtransaction, but its topmost parent is not in `xip`, so its work
  **is** visible. "Has a `pg_subtrans` parent" must not be read as "invisible".
* **B5** rolls back two savepoints inside a transaction that then commits. Those
  children are `ABORTED` in their own right while the parent is `COMMITTED`, so
  the commit log has to be consulted per xid. Inheriting the parent's status
  resurrects 16 dead tuples.

`ROLLBACK TO SAVEPOINT` re-enters a *fresh* subtransaction, so later savepoints
nest inside it and PostgreSQL picks the immediate parents itself. The generator
therefore records only the expected **topmost** ancestor and asserts
`SubTransGetTopmostTransaction` reproduces it, rather than hard-coding a parent
chain the server is free to choose.

### Capturing the SLRU areas

`CHECKPOINT` runs `CheckPointCLOG` and `CheckPointSUBTRANS`, so both areas are
flushed before the directories are copied. The generator then re-decodes the
copied bytes and asserts that

* every xid the heap refers to decodes to the same state `pg_xact_status()`
  reports on the live server, and
* every subxid it drove through a savepoint resolves, through the copied
  `pg_subtrans`, to the top-level transaction it actually belonged to.

That is what makes "these are genuine files from the same cluster" a checked
claim rather than an assertion.

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
  asserts `pg_current_xact_id_if_assigned()` is NULL for both, so neither is
  assigned an xid and neither appears in the snapshot or the commit log.
* **Savepoint groups must not overlap their own parent's other work.** A
  transaction that both creates and later modifies the same tuple gets
  `HEAP_COMBOCID`; every savepoint here touches a disjoint id range, and the
  generator still aborts if the flag appears.
* **The file is copied before the observer runs its final `SELECT`,** so the
  reference query cannot add hint bits to the bytes the solver receives.

## Deliberately excluded tuple cases

These were flagged during proposal review. They are **not implemented**, so the
fixture is built to exclude them and `pg_fixture.py` aborts generation if any
appears. `build/fixture_test.py` re-asserts all four from the solver-facing bytes.

| case | why it is excluded | how it is kept out | assertion |
| --- | --- | --- | --- |
| **Combo CIDs** (`HEAP_COMBOCID`, `0x0020`) | resolving `t_cid` into a `cmin`/`cmax` pair needs the writing backend's in-memory combo array, which no on-disk artefact can supply | no transaction both creates and removes the same tuple; and an external snapshot never consults a command id in the first place | `test_no_combo_command_ids` |

| ~~MultiXact `xmax`~~ | **included since v4**: `pg_multixact/offsets` + `members` are shipped, the oracle resolves members and flags, and eight negatives cover the failure modes | created deliberately with compatible lock modes; burned mxids 1-2; sentinel multi | `test_multixact_*` (9 tests) |
| ~~Frozen tuples~~ | **included since v4**: `VACUUM (FREEZE)` runs before the history; both hint bits set, raw xmin preserved | frozen-then-modified rows are deliberate | `test_frozen_*` (3 tests) |
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
within 10 primary keys of correct.  v3 adds: at least six subtransaction xids on
the pages, a chain at least three hops deep, at least twelve tuple stamps that
need `pg_subtrans`, at least one *visible* subtransaction, and at least two
aborted children under committed parents.

## Reproducibility

PostgreSQL assigns transaction ids and page LSNs itself. Forcing byte-for-byte
equality would mean editing genuine page structures, which would defeat the point
of using a real server, so exact input reproducibility is not a requirement here.

In practice the procedure is stronger than required. Two consecutive
`build/generate_case.py` runs against fresh containers produced
**byte-identical** `heap_pages.bin`, `table_schema.json`, `pg_xact/0000`,
`pg_subtrans/0000`, `pg_multixact/offsets/0000`, `pg_multixact/members/0000`,
`golden.csv` and `expected_state.json` (verified during authoring, for v4 as for
v2 and v3; MultiXactIds allocate as deterministically as xids do). The canonical digest of the expected state is
`73587226df39aea84327e50a8f0c1fc3ec4325c47b74496fda47aa36eebc073e`.

The SLRU segments reproduce for the same reason the heap does: a fresh `initdb`
starts the xid counter at the same value and the generator drives the same
statements in the same order, so the same transaction ids land in the same
commit-log and subtransaction-map slots.

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

**Why ship whole SLRU segments rather than just the needed entries?** Because a
segment is what PostgreSQL writes. Trimming it to the referenced xids would be a
custom format, and the size argument does not bite: `pg_xact` is two bits per
transaction and `pg_subtrans` four bytes, so both areas are one 8 KiB page each.
The segments cover every transaction in the cluster, which leaks nothing - two
status bits per xid is not an answer.

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

**Could a solver point PostgreSQL at these files?** No. There is no
`pg_control`, no catalog, no `base/` directory and no server in the image; the
segments are evidence to decode, not a cluster to start.

**Row order:** genuinely free. The verifier hashes rows keyed by primary key and
separately asserts that reversing and re-sorting the records does not change the
verdict.
