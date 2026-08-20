# Internal notes — how fixture v5 is built (never solver-facing)

## Pipeline

```
build/generate_case.py
  └─ docker run postgres:16  (incident configuration:
       shared_buffers=2MB, synchronous_commit=off, wal_writer_delay=10s,
       checkpoint_timeout=1d, autovacuum=off, fsync=on, full_page_writes=off)
       └─ build/pg_fixture.py   (inside the container, as postgres)
            phase 1: pre-incident history  → CHECKPOINT (clog page 0 durable)
            burn:    xids to 32970 via subtransactions (clog page boundary 32768)
            phase 2: incident history on clog page 1 (never written back)
            open writers + heartbeat → repeatable-read snapshot captured
            targeted WAL flush + probes → the designed hint bits appear
            pg_prewarm eviction → heap pages reach disk
            capture: 3 heap files, pg_xact/, pg_subtrans/, schema, golden.csv
  ├─ host-side proof: run the oracle, assert funnel == (…, 1, 1), oracle ==
  │  golden, inferred assignment == server truth, evidence split == design
  ├─ build/make_expected_state.py   → tests/expected_state.json
  ├─ build/make_solution_sh.py      → solution.sh
  └─ build/make_zip.py              → dist/postgres_mvcc_heap_inputs_v5.zip
```

Regenerate with `python3 build/generate_case.py` (Docker required), then
`build/run_all_validation.sh` for the full suite and
`build/run_container_checks.sh` for the image-level nop/oracle checks.

## The incident transactions (designed truth)

Unresolved = zero clog bits on disk, yet completed per the snapshot.
D = direct (a genuine hint bit exists somewhere), I = indirect (no hint
anywhere; constraint reasoning only).

| xid | label | outcome | evidence | what it does |
| --- | --- | --- | --- | --- |
| 32971 | U_A | committed | D (ledger xmin hint) | account update + ledger insert |
| 32972 | U_B | aborted | D (accounts xmax hint) | account delete attempt |
| 32973 | W1 | committed | I ← W2 via FK | inserts account keys 220/221 + a 16-row page-seal batch |
| 32975 | W3 | aborted | I ← W1 via PK | competing insert of key 221 |
| 32977 | W2 | committed | D (ledger xmin hint) | ledger 5010 + tag (221,'gold') referencing W1's key |
| 32978 | W5 | committed | I ← W6 via delete-forcing | deletes account 40 |
| 32979 | W6 | committed | D (ledger xmin hint) | re-inserts account 40 + virgin ledger 5053 |
| 32981 | W7 | aborted | I (error-abort, dup key 44) | tuple written before the unique check failed |
| 32983 | W9 | aborted | I ← PK exclusion | update of account 45 (its hint was overwritten by W8's same-page write) |
| 32984 | W8 | committed | I ← W10 via FK | inserts account keys 230/231 + a 16-row page-seal batch |
| 32986 | W15 | aborted | I ← W8 via PK | competing insert against W8 |
| 32988 | W10 | committed | D (ledger xmin hint) | ledger 5030 + tag (231,'silver') referencing W8's key |
| 32990 | W11 | aborted | I (error-abort, dup tag) | tag (60,'audit') + duplicate tag (61,'flag') |
| 32992 | W12 | committed | D (ledger xmax hint) | deletes ledger 5040 |
| 32993 | W13 | committed | D (ledger xmin hint) | re-inserts ledger 5040 (its own heap insert preceded the btree check, granting the hint via the page-LSN path) |

Chains: hint(W2) → W1 → W3 (depth 3); hint(W10) → W8 → W15 (depth 3);
hint(W6) → W5 (delete-forcing, depth 2); W7/W11/W9 (depth 1–2, PK/error
devices). Split: 7 direct / 8 indirect (53%; floor is max(4, 30%)).
`generate_case.py` asserts the split holds **exactly** — a leaked hint or a
lost hint fails generation.

Open at the snapshot (xip 32994–32996): an update of account 50, an insert of
account 240 + ledger 5060, a delete of account 52 — all invisible, and all
lost if xip is ignored. The heartbeat commit (a burn_seed insert) after the
writers open is what lifts `latestCompletedXid`, so `snapshot_xmax = 32998`
and the writers actually appear in xip; without it the snapshot reads
`32994:32994:` (v2 learned this the hard way, v5 re-learned it).

## The hint-control toolkit (all server-side behaviour, never edits)

* **Suppress**: `synchronous_commit=off` + `wal_writer_delay=10s` — commits
  don't flush WAL, and `SetHintBits` declines to stamp `XMIN_COMMITTED`
  while the commit LSN is unflushed. Probes during this window leave no
  trace.
* **Grant**: a synchronous commit that performs a real write (an INSERT into
  `burn_seed`) — a sync commit that only ran `pg_current_xact_id()` does
  *not* flush (the `wrote_xlog` optimisation; discovered empirically, see
  the flush matrix in the scratch probes). After the flush, one targeted
  index-scan probe per designed-direct transaction stamps exactly the
  intended hints.
* **Abort hints** stamp unconditionally when probed — so aborted designed-
  direct facts need only a probe, and aborted indirect transactions must
  never be probed.
* **Page-LSN exception**: a WAL-logged write to a page raises the page LSN
  past earlier commit LSNs, making their hints stampable. Two consequences
  managed deliberately: W13's hint (its own insert precedes the uniqueness
  probe) is *designed-direct*, and W9's old-version hint was erased by W8's
  overwrite (leaving W9 indirect).
* **Index-scan discipline**: generation sessions run with
  `enable_seqscan=off, enable_bitmapscan=off`, because on tables this small
  the planner would otherwise seqscan and stamp hints on every tuple it
  passes (this leaked W1 to direct twice before the fix).
* **FSM plug**: opportunistic pruning records freed space in the free-space
  map, which routed phase-2 inserts into phase-1 pages — a same-page write
  then opened the page-LSN path for transactions meant to stay indirect.
  Rows 95–134 (`P1_fsm_plug`) saturate that freed space so phase-2 inserts
  land on fresh pages. The 16-row ~500-byte page-seal batches inside W1/W8
  serve the same purpose within phase 2.

## Evidence classification (oracle-side)

For every xid on any tuple: recorded (surviving clog decides — 9 pre-incident
xids), unresolved (zero bits but `snapshot.completed()` — 15 xids), or
irrelevant (genuinely in progress at the snapshot — the three xip writers
plus post-snapshot xids; invisible either way). The classification "zero bits
+ below xmin ⇒ the clog is stale here" is the v5 pivot; a v3/v4 reader calls
those transactions in-progress and drops 71 rows (the two 34-tuple committed
batches W1/W8 plus singles).

## Exclusions (unchanged policy: refuse, don't guess)

No MultiXact (`HEAP_XMAX_IS_MULTI` aborts the oracle), no frozen tuples, no
combo CIDs, no external TOAST, no compressed datums, no wraparound, no
prepared transactions. `build/fixture_test.py` asserts their absence from the
fixture; the oracle and the independent implementation both hard-fail on
them.

## Reproducibility

* **Given the committed artifacts**, everything downstream is deterministic:
  the oracle is a pure function of the six files (3× byte-identical runs
  asserted), the verifier verdict is stable (3× asserted), the ZIP is
  byte-reproducible (fixed timestamps, sorted members, asserted twice per
  validation run).
* **Regeneration** (`--regen`) was measured to be **byte-identical** across
  independent container runs against the same `postgres:16` image (16.15):
  all three heaps, both SLRU segments, the schema, the ZIP and solution.sh
  reproduced with identical SHA-256s. This holds because the history is
  fully scripted, wall-clock timestamps land only in WAL (whose record
  *lengths*, and therefore all page LSNs, are value-independent), and
  `make_zip.py` fixes archive metadata. A future `postgres:16` point
  release could legitimately shift bytes; in that event the asserted
  *logical* invariants still gate every regeneration: the funnel ends at
  exactly (1 assignment, 1 output); the oracle equals PostgreSQL's own
  answer; the inferred assignment equals the server truth xid-for-xid; the
  designed direct/indirect split holds exactly; and 12 ≤ unresolved ≤ 22
  with ≥ 30% indirect. The verifier fixture is regenerated in the same run,
  so the package stays self-consistent either way.

## Files that must never ship

`build/internal/*` (golden.csv, generation_report.json,
inference_report.json, realism_observations.json, negative_scores.json),
`tests/expected_state.json`, `solution/`, `REALISM_AUDIT.md`, this file, and
everything under `build/`. `make_zip.py` enforces the member list and
patterns; `fixture_test.py` and `final_audit.py` re-check leakage byte-wise.
