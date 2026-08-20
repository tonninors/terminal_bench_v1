# Golden solution

Reference implementation: [`solution/golden_recover.py`](solution/golden_recover.py),
wrapped for the container by [`solution.sh`](solution.sh) (generated — edit the
Python and rerun `build/make_solution_sh.py`).

It reads **only** `/app/heap_pages.bin`, `/app/pg_xact/`, `/app/pg_subtrans/`,
`/app/pg_multixact/` and `/app/table_schema.json`. It never opens the hidden reference CSV, never
hard-codes a row, never contacts a database, and contains no lookup table.

```
python3 solution/golden_recover.py \
    --heap        artifacts/heap_pages.bin \
    --pg-xact     artifacts/pg_xact \
    --pg-subtrans artifacts/pg_subtrans \
    --pg-multixact artifacts/pg_multixact \
    --schema      artifacts/table_schema.json \
    --out         /app/recovered.csv --report
```

---

## Step 1 — read the schema and the snapshot

`table_schema.json` gives the column order, each column's PostgreSQL type and
nullability, the primary key, the block size and the target snapshot. Build one
`Attr` per column carrying `typlen` and `attalign`, derived from the type name:

| type | typlen | attalign | decoding |
| --- | --- | --- | --- |
| `integer` | 4 | 4 | signed little-endian |
| `smallint` | 2 | 2 | signed little-endian |
| `boolean` | 1 | 1 | non-zero byte is true |
| `date` | 4 | 4 | signed day count from 2000-01-01 |
| `text`, `character varying(n)` | −1 | 4 | varlena, UTF-8 |

The snapshot is `(snapshot_xmin, snapshot_xmax, snapshot_xip)`.

## Step 2 — decode the commit log and the subtransaction map

Both are SLRU areas: a directory of fixed-size segment files, each holding 32
pages of 8192 bytes, named with the segment number in four hex digits.

**`pg_xact`** packs two bits per transaction id — 32768 ids per page:

```python
CLOG_XACTS_PER_PAGE = 8192 * 4
pageno          = xid // CLOG_XACTS_PER_PAGE
segno, page     = divmod(pageno, 32)
byte            = segment[segno][page*8192 + (xid % CLOG_XACTS_PER_PAGE)//4]
status          = (byte >> ((xid % 4) * 2)) & 0x03
# 0 IN_PROGRESS   1 COMMITTED   2 ABORTED   3 SUB_COMMITTED
```

**`pg_subtrans`** stores a four-byte parent transaction id per entry — 2048 ids
per page. Zero means the transaction is top-level:

```python
SUBTRANS_XACTS_PER_PAGE = 8192 // 4
pageno       = xid // SUBTRANS_XACTS_PER_PAGE
segno, page  = divmod(pageno, 32)
parent       = uint32_le(segment[segno], page*8192 + (xid % SUBTRANS_XACTS_PER_PAGE)*4)
```

Two derived operations matter, and they are **not** the same thing:

* `TransactionIdDidCommit(xid)` — the commit log is authoritative **per xid**.
  A `SUB_COMMITTED` entry is the one exception: the subtransaction finished but
  its parent had not, so follow the parent. Everything else answers directly.
  In particular a savepoint that was rolled back inside a transaction that later
  committed is `ABORTED` in its own right.
* `SubTransGetTopmostTransaction(xid)` — walk `pg_subtrans` parents until one is
  zero. Savepoints nest, so the map records the *immediate* parent and the walk
  takes several hops; a parent id is always lower than its child, which bounds
  the loop. This is what the **snapshot** must be tested against.

## Step 3 — split the file into 8192-byte blocks

`len(heap_pages.bin) % 8192 == 0`; block *n* is bytes `[n*8192, (n+1)*8192)`, in
relation block-number order starting at block 0.

## Step 4 — parse each page header

`PageHeaderData` is 24 bytes:

```
0   pd_lsn              8   (XLogRecPtr)
8   pd_checksum         2
10  pd_flags            2
12  pd_lower            2   <- end of the line pointer array
14  pd_upper            2
16  pd_special          2
18  pd_pagesize_version 2   <- 0x2000 | 4  (8192, layout version 4)
20  pd_prune_xid        4
24  pd_linp[]               <- ItemIdData array starts here
```

The number of line pointers is `(pd_lower - 24) / 4`. Sanity-check
`24 <= pd_lower <= pd_upper <= pd_special <= 8192`.

## Step 5 — decode the line pointer array, and respect `lp_flags`

Each `ItemIdData` is one little-endian `uint32` bitfield:

```
lp_off   = raw & 0x7FFF          # byte offset of the tuple within the page
lp_flags = (raw >> 15) & 0x3     # 0 UNUSED, 1 NORMAL, 2 REDIRECT, 3 DEAD
lp_len   = (raw >> 17) & 0x7FFF  # tuple length in bytes
```

**Only `LP_NORMAL` slots hold a tuple.** This is where HOT matters:

* `LP_REDIRECT` is the root of a HOT chain after pruning. Its `lp_off` is an
  **OffsetNumber**, not a byte offset, and the slot itself stores nothing —
  parsing it as a tuple reads unrelated page bytes, and following it and emitting
  the target produces a duplicate of a row already reached directly. Skip it.
* `LP_DEAD` and `LP_UNUSED` have no storage. Skip them.

Every live version — including heap-only tuples that have no index entry — is
reachable by simply visiting every `LP_NORMAL` slot, so no chain following is
needed once redirects are skipped. This fixture has 725 `LP_NORMAL`, 15
`LP_REDIRECT` and 13 `LP_DEAD` slots.

Both directions of getting this wrong are punished. Walking an `LP_REDIRECT` as
a tuple duplicates 15 keys. Going the other way and *skipping* heap-only tuples,
on the theory that they are internal HOT bookkeeping, loses 102 visible rows: a
`HEAP_ONLY_TUPLE` is a complete row version that merely has no index entry, and
on these pages it is very often the version the snapshot can see.

## Step 6 — parse the tuple header

```
0   t_xmin        4
4   t_xmax        4
8   t_field3      4   (t_cid or t_xvac)
12  t_ctid        6   (block hi16, block lo16, offset16)
18  t_infomask2   2   (natts in the low 11 bits, HOT flags at the top)
20  t_infomask    2
22  t_hoff        1
23  t_bits[]          (null bitmap, only when HEAP_HASNULL)
```

`natts = t_infomask2 & 0x07FF`. If `t_infomask & HEAP_HASNULL (0x0001)`, the null
bitmap occupies `ceil(natts/8)` bytes starting at byte 23 — bit *i* **set** means
attribute *i* is **not** null. `t_hoff` is `MAXALIGN` of the header plus bitmap
and is where the user data begins; use `t_hoff`, do not recompute it.

## Step 7 — walk the attributes

Start the cursor at `t_hoff` and, for each column in schema order:

1. If the column index is `>= natts`, the attribute is absent (it would have been
   added by a later `ALTER TABLE`): NULL.
2. If the null bitmap says NULL: emit NULL and **consume no bytes and apply no
   alignment**. This is the step that silently corrupts every later column if it
   is done wrong.
3. Otherwise align and read:
   * fixed-length: round the cursor up to `attalign`, read `typlen` bytes,
     advance by `typlen`;
   * varlena: **do not align unconditionally.** PostgreSQL's `att_align_pointer`
     skips the padding when the byte at the cursor is non-zero, which is exactly
     the short-header case:
     * `b & 0x01` → 1-byte header; total size `b >> 1`, payload is the next
       `size - 1` bytes;
     * `b == 0x01` → out-of-line TOAST pointer (does not occur here — the prompt
       states every value is inline);
     * otherwise a 4-byte header, `uint32` little-endian; `size = word >> 2`, and
       `word & 0x03 == 0x02` would mean compressed (also absent here).
     Advance by the total size.

## Step 7b — decode the MultiXact metadata

`pg_multixact` is two more SLRU areas with a **third distinct geometry**:

* `offsets`: one 32-bit member index per MultiXactId, 2048 per page. Member
  index 0 is **reserved** (so a zero entry can mean "never written"): the first
  multi's list starts at index 1. The member list of multi M spans
  `[offsets[M], offsets[M+1])` — the *next* multi's entry is the bound, which is
  why the cluster contains one trailing multi created after the last one the
  pages reference.
* `members`: groups of four — 4 status-flag bytes followed by 4 xids, 20 bytes
  per group, 409 groups (1636 members) per page.

The status flag per member is a `MultiXactStatus`: 0 ForKeyShare, 1 ForShare,
2 ForNoKeyUpdate, 3 ForUpdate are **lockers**; 4 NoKeyUpdate, 5 Update are
**updaters** (`ISUPDATE_from_mxstatus`: status ≥ 4). A multi has at most one
update member.

## Step 8 — decide visibility (`HeapTupleSatisfiesMVCC`)

First, the snapshot predicate — `XidInMVCCSnapshot`, i.e. "was this transaction
still running when the snapshot was taken?":

```python
def in_progress(xid, snap, log):
    if xid >= snap.xmax:  return True    # had not completed; may not have started
    if xid <  snap.xmin:  return False   # had already completed
    if xid in snap.xip:   return True    # a running top-level transaction
    top = log.topmost(xid)               # a subtransaction? ask pg_subtrans
    if top == xid:        return False   # top-level and not listed: finished
    if top >= snap.xmax:  return True
    if top <  snap.xmin:  return False
    return top in snap.xip
```

Note two things. First, a transaction the commit log reports as `committed` is
still invisible to this snapshot if it is at or above `snapshot_xmax`, or if it
appears in `snapshot_xip`.

Second, and this is where most of the difficulty now lives: **`snapshot_xip`
lists top-level transaction ids only.** `pg_current_snapshot()` does not export
the subtransaction array. A tuple written inside a savepoint carries the
*subtransaction's* xid, which appears in no xip entry and — once its parent
commits — reads `COMMITTED` in the commit log. Taken at face value it looks like
an ordinary finished transaction whose work is visible. It is not: resolve it
through `pg_subtrans` first, and test the topmost parent.

Then the rule itself:

```python
# --- the inserting transaction ---
if (infomask & HEAP_XMIN_FROZEN) == HEAP_XMIN_FROZEN:   # 0x0300; absent here
    inserted_ok = True                                  # frozen: older than all
elif infomask & HEAP_XMIN_INVALID:                      # 0x0200 hint: aborted
    inserted_ok = False
else:
    inserted_ok = did_commit(xmin) and not in_progress(xmin, snap, log)
if not inserted_ok:
    return False

# --- the deleting transaction ---
if infomask & HEAP_XMAX_INVALID:   return True   # 0x0800 hint: xmax is void
if xmax == 0:                      return True
if infomask & HEAP_XMAX_IS_MULTI:                # 0x1000: xmax is a MultiXactId
    if infomask & HEAP_XMAX_LOCK_ONLY:
        return True                              # every member is a locker
    u = multixact_updater(xmax)                  # offsets -> members -> flags
    if u is None:                    return True
    if not did_commit(u):            return True # updater aborted / running
    return not in_progress(u, snap, log)         # snapshot decides, via subtrans
if infomask & HEAP_XMAX_LOCK_ONLY: return True   # 0x0080: a row lock, not a delete
if not did_commit(xmax):           return True   # deleter aborted or still running
return not in_progress(xmax, snap, log)          # deleted after the snapshot? visible
```

The `IS_MULTI` branch comes **before** any commit-log lookup of `t_xmax`: an
mxid is not a transaction id, and in this cluster the mxids 3–14 collide with
committed bootstrap xids, so a missing branch produces confident wrong answers
rather than errors. The update member is an ordinary xid afterwards — its own
clog bits (a rolled-back savepoint updater is ABORTED under a COMMITTED
parent), its own `pg_subtrans` ancestry, and the snapshot all apply.

Six details carry real weight here:

* **The mxid/xid collision is silent.** Misreading a multi as an xid loses 56
  keys with no error; treating every multi as a lock duplicates 11; letting any
  committed member kill loses 52; skipping `pg_subtrans` for the update member
  loses 30; skipping the snapshot for it loses 28; shifting the reserved
  offset-0 base scrambles member windows for 22.

* **A subtransaction is one transaction for the commit log and another for the
  snapshot.** `pg_xact` decides, per xid, whether that particular savepoint's
  work survived; `pg_subtrans` decides which top-level transaction the snapshot
  should be tested against. Using the parent for both resurrects rolled-back
  savepoints (16 dead tuples here); using the child for both makes an in-flight
  transaction's work visible (24 tuple stamps here).

* **`xmax != 0` is not a deletion, and this is the single biggest trap.** 53
  tuples in this fixture were locked by `SELECT ... FOR UPDATE`, `FOR NO KEY
  UPDATE`, `FOR SHARE` or `FOR KEY SHARE`. Their `xmax` names a real
  transaction, and for 33 of them that transaction both committed *and* is
  plainly visible to the target snapshot - so every rule of the form "xmax
  committed means the row is gone" throws them away. What keeps them alive is
  `HEAP_XMAX_LOCK_ONLY`, and nothing else: none of these tuples carries the
  `HEAP_XMAX_INVALID` hint, because their pages were never pruned after the
  locker finished. The infomask has to be read.
* **Hint bits are not the commit log.** 128 of 725 tuples have no
  `HEAP_XMIN_COMMITTED`, and 12 of the 75 tuples inserted by aborted
  transactions carry no `HEAP_XMIN_INVALID` either. Read `pg_xact`; the hints
  only ever confirm it.
* **An aborted deleter leaves the row visible**, and an aborted inserter's tuple
  is dead no matter what its `xmax` says. For 47 keys the tuple with the
  greatest `xmin` is exactly such a dead version.
* **The lock modes look different on the page.** `FOR KEY SHARE` sets
  `HEAP_XMAX_KEYSHR_LOCK`, `FOR UPDATE` and `FOR NO KEY UPDATE` set
  `HEAP_XMAX_EXCL_LOCK`, `FOR SHARE` sets both. All of them also set
  `HEAP_XMAX_LOCK_ONLY`, which is why testing that bit - plus the legacy
  exclusive-lock arm of `HEAP_XMAX_IS_LOCKED_ONLY` - covers every case.

## Step 9 — one row per primary key

Collect the visible tuples, key them by the primary key columns, and assert that
no key appears twice. MVCC guarantees at most one visible version per key; two
would mean a parsing or visibility bug, not an ambiguous snapshot. Here 855
physical tuples over 482 distinct keys reduce to exactly 429 visible rows.

40 keys have three or more physical versions still on the page and the deepest
chain is seven tuples long, so this reduction is not "take the last one": for 12
keys the visible version sits strictly inside its chain, with both older and
newer versions present on the same page.

## Step 10 — write the CSV

UTF-8, `csv.writer` (RFC 4180 quoting), header = the schema columns in order,
then one record per visible key. NULL is the two characters `\N`; booleans are
`t`/`f`; dates are `YYYY-MM-DD`; integers are decimal. Row order is free — the
oracle sorts by primary key for determinism.

---

## What the oracle reports on this fixture

```
blocks                                            9
line pointers      LP_NORMAL 855, LP_REDIRECT 23, LP_DEAD 13
physical tuples                                 855
visible rows                                    429
snapshot                                        804:842:804,810,814,816,818,820,821,827,829,830,831,832,833,836,838
multixacts resolved                             12 (ids 3-14)
tuples with a MultiXact xmax                    67
  locker-only / with updater                    18 / 49
  updater is a subtransaction                   23
  updater decided by the snapshot alone         28
frozen tuples / frozen with xmax                422 / 411
subtransaction xids on the pages                13 (chains up to 3 hops)
keys whose visible version is NOT the newest xmin 179
single-xid lock-only tuples kept                  57
hint-bit conflicts with the commit log            0
```

## Independent confirmation

`build/internal/golden.csv` was produced by the PostgreSQL server itself, by
running `SELECT * FROM account_ledger ORDER BY account_id` inside the very
`REPEATABLE READ` transaction that owns the target snapshot. The oracle's output
is **byte-identical** to it, on all 429 rows, (`build/run_all_validation.sh` step 5,
`build/fixture_test.py::test_the_page_level_answer_equals_the_postgresql_reference`).
The page-level recovery and the database engine agree on all 429 rows.

`build/negatives/alt_independent_parser.py` is a second recovery written from
scratch — different parser structure, different formulation of the visibility
rules, and output deliberately shaped differently (CRLF, `true`/`false`,
descending row order). It also passes, confirming the verifier grades the outcome
rather than the method.
