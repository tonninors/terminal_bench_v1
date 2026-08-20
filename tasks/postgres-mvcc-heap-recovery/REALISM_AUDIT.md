# Realism Audit — the stale clog tail is authentic (internal)

This document records the empirical evidence that fixture v5's central
artifact — an on-disk `pg_xact` that lacks outcomes for transactions whose
heap effects are on disk — was produced by PostgreSQL's own write-back
behaviour at a live server, **not** by truncating, zero-filling or otherwise
editing a complete clog after the fact. No heap byte, clog byte, hint bit or
transaction metadata was ever edited by hand; every solver-facing byte was
written by the PostgreSQL 16 server itself and copied verbatim.

## Why the state is reachable at all (mechanism)

* PostgreSQL records each transaction outcome in three places with different
  durability schedules: the WAL (flushed per policy), the in-memory clog SLRU
  page (written back only at checkpoints or page eviction), and per-tuple
  hint bits (opportunistic). After a crash, recovery replays the WAL to
  repair the clog — so the on-disk clog alone is *expected* to be incomplete
  at any crash instant. Losing the WAL volume makes that incompleteness
  permanent. This is exactly the incident the task states.
* With `synchronous_commit = off`, commits do not wait for a WAL flush, so a
  commit can be visible (and its heap effects flushable) long before its WAL
  — and therefore its hint bits — become grantable. `SetHintBits()` refuses
  to stamp `XMIN_COMMITTED` unless the commit LSN is flushed (or the page LSN
  already exceeds it); abort hints are stamped unconditionally when probed.
  This gives the generator *authentic* control over which transactions carry
  hints: flush the WAL and probe (hint appears), or don't (hint provably
  cannot appear).
* Heap pages reach disk independently of clog pages: evicting them from a
  tiny `shared_buffers` writes them out, while the clog page for recent xids
  stays dirty in memory until a checkpoint — which the configuration delays
  for a day and the history never triggers after the burn.

## Stage-0 authenticity gate (live probe, one continuous run)

`scratchpad gate_probe.py` ran against a live `postgres:16` container under
the incident configuration and verified, in a single uninterrupted session:

| check | result |
| --- | --- |
| G1 heap-on-disk: after `pg_prewarm`-driven eviction, the post-checkpoint transaction's rows are present in the on-disk relation file | PASS |
| G2 clog-stale: the on-disk `pg_xact` bits for those same xids read 0 (in progress) and the file did not grow after the checkpoint | PASS |
| G3 server-knows: `pg_xact_status()` on the live server reports the true committed/aborted outcomes | PASS |
| G4 wal-recoverable: `pg_waldump` finds the COMMIT/ABORT records in the WAL segments — crash recovery *would* repair the clog if the WAL existed | PASS |
| G5 hint-suppression: probing a committed-but-unflushed tuple does NOT stamp `XMIN_COMMITTED` (SetHintBits declines) | PASS |
| G5b page-LSN path: a later WAL-logged write to the same page raises the page LSN past the commit LSN, after which the hint becomes stampable — the documented exception, managed by the generator | PASS |
| G6 hint-grant: a real-write synchronous commit (a plain `pg_current_xact_id()` call writes WAL but does not flush it — the `wrote_xlog` optimisation) flushes the WAL, after which a targeted index probe stamps `XMIN_COMMITTED` | PASS |
| G7 abort-auto-hint: probing a tuple of a rolled-back transaction stamps `XMIN_INVALID` with no flush needed | PASS |

Gate verdict: **PASS** (all checks in one continuous run). The design was
only implemented after this gate passed, per the stage-0 requirement.

## Incident configuration (recorded from the live server)

From `build/internal/realism_observations.json` (`settings`, written by
`SHOW`-ing each GUC at generation time):

* `shared_buffers = 2MB` — small buffer pool so heap eviction is drivable
* `synchronous_commit = off` — commits do not flush WAL (normal production
  option; the crash then loses recent outcomes, which is the incident)
* `wal_writer_delay = 10s` — the WAL writer does not flush behind our back
  during the short capture window
* `fsync = on` — everything that IS written back is durable; nothing about
  the incident relies on disabled durability
* `checkpoint_timeout = 1d`, `autovacuum = off` — no background process
  advances the clog write-back or freezes tuples during the history

## Generation-time observations (this fixture's actual bytes)

Recorded in `build/internal/realism_observations.json` and re-asserted by
`build/final_audit.py` on every audit run:

* `burn`: xids 758 → 32970 consumed via subtransactions so the incident
  transactions (32971–32993) land on clog page 1 while the pre-incident
  history lives on page 0.
* `checkpoint.clog_size_after = 16384` and `capture.clog_file_size = 16384`:
  the on-disk clog was two pages at the pre-incident checkpoint and **never
  grew afterwards** — the tail is stale because it was never written, not
  because anything was removed.
* `checkpoint.highest_pre_incident_xid = 32970 <` every unresolved xid: the
  staleness is exactly the post-checkpoint tail.
* `stale_clog_xids_verified`: at capture, the raw bytes of `pg_xact/0000`
  were read back and the two status bits verified zero for each of the 15
  unresolved xids — while the same file resolves the pre-incident xids on
  page 0.
* `waldump_outcome_records_found = 17 ≥ 15`: `pg_waldump` over the live
  server's WAL found the durable COMMIT/ABORT records for the unresolved
  xids (samples stored in the observations file). The outcomes existed; only
  the artifact that survived the modelled incident lacks them.
* `capture.wal_insert_write_flush_lsn`: insert, write and flush LSNs were
  equal at capture (the generator flushes WAL before its own final probes,
  then captures files — the solver-facing clog had already stopped changing
  at its checkpoint size).

## What was NOT done

* No `truncate`, `dd`, Python slicing or any other post-capture surgery on
  `pg_xact`, `pg_subtrans` or any heap file. The files are byte-for-byte
  what `docker cp` took from the data directory.
* No hint bit was ever set or cleared by hand. Hints present in the heaps
  were stamped by PostgreSQL during the history (G6/G7 mechanics); hints
  absent are absent because SetHintBits genuinely declined (G5) or because
  no probe touched the tuple. The absence of a hint is therefore never
  treated as evidence of an outcome by the oracle — only presence is.
* No WAL, no server truth table, and no decoded transaction state ships to
  the solver; `pg_waldump` output exists only in the internal observations
  file.
* The planner settings used by the *generation sessions*
  (`enable_seqscan = off`, `enable_bitmapscan = off`) are ordinary client
  GUCs controlling which plans the probe queries take; every byte the solver
  sees was still produced by the server's normal executor and buffer
  machinery.
