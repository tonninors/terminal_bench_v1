# Golden solution

Reference implementation: [`solution/golden_recover.py`](solution/golden_recover.py),
wrapped for the container by [`solution.sh`](solution.sh) (generated — edit the
Python and rerun `build/make_solution_sh.py`).

It reads **only** `/app/heap_pages.bin`, `/app/tx_status.csv` and
`/app/table_schema.json`. It never opens the hidden reference CSV, never
hard-codes a row, never contacts a database, and contains no lookup table.

```
python3 solution/golden_recover.py \
    --heap artifacts/heap_pages.bin \
    --tx   artifacts/tx_status.csv \
    --schema artifacts/table_schema.json \
    --out  /app/recovered.csv --report
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

## Step 2 — read the transaction states

`tx_status.csv` is `xid,status` with `status` in `{committed, aborted,
in_progress}`. Load it into a dict. It is authoritative for *whether* a
transaction finished and how; it says nothing about *when*, which is what the
snapshot is for.

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
needed once redirects are skipped. This fixture has 323 `LP_NORMAL`, 37
`LP_REDIRECT` and 34 `LP_DEAD` slots.

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

## Step 8 — decide visibility (`HeapTupleSatisfiesMVCC`)

First, the snapshot predicate — `XidInMVCCSnapshot`, i.e. "was this transaction
still running when the snapshot was taken?":

```python
def in_progress(xid, snap):
    if xid >= snap.xmax:  return True    # had not completed; may not have started
    if xid <  snap.xmin:  return False   # had already completed
    return xid in snap.xip               # the explicit running list decides
```

Note what this means: a transaction that `tx_status.csv` reports as `committed`
is still invisible to this snapshot if it is at or above `snapshot_xmax`, or if
it appears in `snapshot_xip`. Both cases are present in this fixture.

Then the rule itself:

```python
# --- the inserting transaction ---
if (infomask & HEAP_XMIN_FROZEN) == HEAP_XMIN_FROZEN:   # 0x0300; absent here
    inserted_ok = True                                  # frozen: older than all
elif infomask & HEAP_XMIN_INVALID:                      # 0x0200 hint: aborted
    inserted_ok = False
else:
    inserted_ok = status[xmin] == "committed" and not in_progress(xmin, snap)
if not inserted_ok:
    return False

# --- the deleting transaction ---
if infomask & HEAP_XMAX_INVALID:   return True   # 0x0800 hint: xmax is void
if xmax == 0:                      return True
if infomask & HEAP_XMAX_LOCK_ONLY: return True   # 0x0080: a row lock, not a delete
if infomask & HEAP_XMAX_IS_MULTI:  ...           # 0x1000; absent here by design
if status[xmax] != "committed":    return True   # deleter aborted or still running
return not in_progress(xmax, snap)               # deleted after the snapshot? visible
```

Three details carry real weight here:

* **`xmax != 0` is not a deletion.** Three tuples in this fixture were locked by
  a committed `SELECT ... FOR UPDATE`; their `xmax` names a committed
  transaction, but `HEAP_XMAX_LOCK_ONLY` (and the `HEAP_XMAX_INVALID` hint
  PostgreSQL later stamps on them) marks them live.
* **Hint bits are not the commit log.** 46 of 323 tuples have no
  `HEAP_XMIN_COMMITTED`, and only 4 of the 40 tuples inserted by aborted
  transactions carry `HEAP_XMIN_INVALID`. Use `tx_status.csv`; the hints only
  ever confirm it.
* **An aborted deleter leaves the row visible**, and an aborted inserter's tuple
  is dead no matter what its `xmax` says.

## Step 9 — one row per primary key

Collect the visible tuples, key them by the primary key columns, and assert that
no key appears twice. MVCC guarantees at most one visible version per key; two
would mean a parsing or visibility bug, not an ambiguous snapshot. Here 323
physical tuples over 236 distinct keys reduce to exactly 210 visible rows.

## Step 10 — write the CSV

UTF-8, `csv.writer` (RFC 4180 quoting), header = the schema columns in order,
then one record per visible key. NULL is the two characters `\N`; booleans are
`t`/`f`; dates are `YYYY-MM-DD`; integers are decimal. Row order is free — the
oracle sorts by primary key for determinism.

---

## What the oracle reports on this fixture

```
blocks                                            4
line pointers      LP_NORMAL 323, LP_REDIRECT 37, LP_DEAD 34, LP_UNUSED 0
physical tuples                                 323
distinct primary keys on disk                   236
keys with several physical versions              87
visible rows                                    210
snapshot                          xmin 770, xmax 774, xip [770, 771, 772]
keys whose visible version is NOT the newest xmin 81
tuples kept despite a non-zero lock-only xmax      3
frozen tuples seen                                0
hint-bit conflicts with tx_status                 0
```

## Independent confirmation

`build/internal/golden.csv` was produced by the PostgreSQL server itself, by
running `SELECT * FROM account_ledger ORDER BY account_id` inside the very
`REPEATABLE READ` transaction that owns the target snapshot. The oracle's output
is **byte-identical** to it (`build/run_all_validation.sh` step 5,
`build/fixture_test.py::test_the_page_level_answer_equals_the_postgresql_reference`).
The page-level recovery and the database engine agree on all 210 rows.

`build/negatives/alt_independent_parser.py` is a second recovery written from
scratch — different parser structure, different formulation of the visibility
rules, and output deliberately shaped differently (CRLF, `true`/`false`,
descending row order). It also passes, confirming the verifier grades the outcome
rather than the method.
