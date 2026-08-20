# Golden Solution — postgres-mvcc-heap-recovery (fixture v5)

Two independent constructions of the correct answer exist in this package,
plus the answer PostgreSQL itself reported. All three agree row for row.

1. **`solution/golden_recover.py`** (embedded verbatim in `solution.sh`) —
   the oracle: parses the three heaps, reads the SLRU files, classifies the
   evidence, exhaustively enumerates outcome assignments, and emits the
   unique surviving state.
2. **`build/negatives/alt_independent_parser.py`** — a second implementation
   written from scratch (its own page/tuple decoder, its own SLRU readers, a
   DFS instead of product enumeration) that emits CRLF, TRUE/FALSE and
   descending row order, proving the verifier grades the outcome only.
3. **`build/internal/golden.csv`** — produced by the live PostgreSQL server
   itself, inside the repeatable-read transaction that owns the target
   snapshot, at generation time. Never consulted by the oracle.

## The oracle, step by step

### 1. Parse the heaps (v3-level work)

For each of the three relations named in `table_schema.json`:

* split the file into 8192-byte blocks; check `pd_pagesize_version`
  (0x2004 = 8192 bytes, layout 4) and the header offsets;
* walk the line pointer array. `LP_NORMAL` slots are parsed; `LP_REDIRECT`
  and `LP_DEAD` carry no tuple of their own; `LP_UNUSED` is skipped;
* decode each tuple header (`t_xmin`, `t_xmax`, `t_infomask`, `t_infomask2`,
  `t_hoff`), honouring the null bitmap sized by the *tuple's* attribute
  count, alignment per type, and 1-byte/4-byte varlena headers;
* refuse (rather than guess) on anything out of scope: `HEAP_XMAX_IS_MULTI`,
  combo CIDs, external TOAST, compressed datums.

### 2. Read the transaction metadata

* `pg_xact/`: two status bits per xid (00 in-progress, 01 committed,
  10 aborted, 11 subcommitted → the parent decides via `pg_subtrans`);
* `pg_subtrans/`: four-byte parent per xid; `topmost()` follows the chain;
* the snapshot: an xid is in progress at the snapshot iff `xid >= xmax`, or
  it (or its topmost ancestor) is listed in `xip`. Below `xmin` it had
  completed.

### 3. Classify every referenced xid (the v5 pivot)

For each xid stamped on any tuple of any relation:

* **recorded** — the surviving clog decides it (9 xids, all pre-incident);
* **unresolved** — the clog bits are zero *but the snapshot proves the
  transaction finished* (`xid < snapshot_xmin` and not in xip): the on-disk
  clog is stale for it. 15 such xids (32971–32993, skipping the open ones);
* **irrelevant** — unrecorded and genuinely in progress at the snapshot
  (the three open writers in `xip`, plus post-snapshot xids): invisible
  either way, no outcome needed.

### 4. Collect the authoritative direct evidence

Hint bits are facts stamped by the server: `HEAP_XMIN_COMMITTED`/`INVALID`
on any tuple, and `HEAP_XMAX_COMMITTED`/`INVALID` on tuples whose xmax is
not lock-only. One fact per xid, valid across all relations (atomicity).
Contradictions abort the run; none exist. **Absence of a hint is not
evidence** — SetHintBits legitimately declines to stamp unflushed commits.
This resolves 7 of the 15 unresolved xids (5 of the 7 facts live on
`ledger_entries`).

### 5. Enumerate complete candidate states

For each of the 2^15 assignments of committed/aborted to the unresolved xids
(the hint facts prune to 256 candidates immediately):

* compute the visible tuple set of **all three relations** under standard
  MVCC visibility (xmin committed and not in-progress at the snapshot; xmax
  absent, invalid, lock-only, not committed, or in-progress at the
  snapshot);
* keep the assignment only if the complete state satisfies every declared
  constraint: primary keys (accounts, ledger_entries and the composite
  account_tags key each appear at most once), NOT NULL, the row-local CHECK
  expressions, and both foreign keys (every visible child references a
  visible account).

The funnel, recorded in `build/internal/inference_report.json` and re-proven
on every validation run:

```
32768 assignments → 256 after hint facts → 9 after PK/UNIQUE → 1 after all
constraints; distinct outputs among survivors: 1
```

The oracle **aborts** ("refusing to guess") unless exactly one assignment
and one output survive. The unique survivor equals the outcome the server
actually took for all 15 xids, and its `accounts` projection equals
`golden.csv` (199 rows).

### 6. Emit

The visible `accounts` rows are written as RFC 4180 CSV in the schema column
order — integers in decimal, booleans `t`/`f`, dates `YYYY-MM-DD`, text
literal, NULL as `\N` — sorted by primary key (any order passes).

## Why the indirect chains resolve (worked examples)

* **W1 → W3** (depth 3): W2's ledger row is hint-committed; its account
  reference is a key only W1 inserts, so FK forces W1 committed. W1 and W3
  both insert account key 221; two committed inserts of one key are
  impossible, so W3 aborted.
* **W5** : W6 is hint-committed and re-inserts account 40; the only way key
  40 is not duplicated is that W5's delete of the old version committed.
* **W8 → W15** (depth 3): W10 is hint-committed and tags account 231, which
  only W8 provides → W8 committed; W8 and W15 then collide on a key → W15
  aborted.
* **W7, W11**: error-aborted transactions whose heap tuples duplicate
  hinted-visible keys (PostgreSQL writes the tuple before the unique check
  fails); committing them is impossible, so they abort.
* **W9**: update whose old version's hint was destroyed by a later
  same-page overwrite; PK exclusion still forces it aborted.

## Determinism

* The oracle is a pure function of the six solver files; three consecutive
  runs produce byte-identical output (`build/harness_test.py`).
* The enumeration order is fixed (sorted xids, committed before aborted),
  and with exactly one survivor the order cannot matter anyway.
* `solution.sh` embeds the oracle verbatim; `build/harness_test.py` fails if
  it drifts from `solution/golden_recover.py`.
