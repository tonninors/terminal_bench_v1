# Internal notes — how the artifacts are made (NOT for the solver)

> This file is internal-only. It must never be shipped in the solver ZIP or
> copied into the task container.

Generator: `build/generate_case.py`. Support modules: `build/ledger_schema.py`
(schema + synthetic data), `build/walkit.py` (WAL structures, checksums,
canonical row encoding). Seed: `20260819`. Built against SQLite 3.45.1.

Run `python3 build/generate_case.py` from the task directory. It wipes
`build/_work/`, rebuilds everything and refuses to finish unless all of its
self-checks hold.

## 1. Base state

A WAL-mode database is created with `page_size=4096`, `auto_vacuum=NONE`,
`wal_autocheckpoint=0` and `foreign_keys=ON`, populated with 64 accounts, 320
journal entries and 640 postings (triggers generate the audit rows and maintain
`accounts.balance_minor`), then `PRAGMA wal_checkpoint(TRUNCATE)` folds
everything into the main file. The main database therefore contains the *base*
state and nothing else: 323 pages.

## 2. The three committed transactions (T1, T2, T3)

Written on one connection with auto-checkpointing disabled, so all of their
page changes stay in the log:

* **T1** — 26 new journal entries with postings; grows the file.
* **T2** — amends narratives, memos and FX rates on 34 existing entries
  (rewriting overflow pages) and re-opens 6 accounts. Overwrites pages T1 wrote.
* **T3** — 14 more entries with postings, settles 18 pending entries
  (`pending → posted`, which also moves rows in and out of the partial index)
  and freezes accounts 11 and 29. **This is the answer.**

Frame layout that results: 201 frames, commit frames at 0-based indexes 47, 143
and 200; the commit at 200 declares a database size of 382 pages.

The crash snapshot is taken by copying `ledger.db` and `ledger.db-wal` **while
the connection is still open** — closing the connection would checkpoint. That
byte pair is precisely what a power loss right after T3's commit leaves behind.

## 3. The golden database

The pristine snapshot is copied aside, opened by SQLite, checkpointed with
`wal_checkpoint(TRUNCATE)` and switched to `journal_mode=DELETE`. SQLite's own
recovery produces the ground truth, so the expected state is not something this
generator invented: `build/internal/golden.db`, 64 / 360 / 720 / 740 rows in
`accounts` / `journal_entries` / `postings` / `audit_log`.

## 4. The crash tail (T4) — why it is *not* committed

T4's page images are real SQLite output, harvested by replaying the snapshot on
a scratch copy and committing there: 9 new entries with postings, 6 existing
entries voided, 4 entries deleted (cascading into `postings` and firing the
balance/audit triggers) and 3 accounts frozen. That yields 54 frames, of which
exactly one — the last — is a commit frame.

**The commit marker is then removed**: `db_size` in that frame's header is set
to 0. In the WAL format a transaction is committed when, and only when, a frame
carries a non-zero "database size in pages" field. With that field zeroed, the
54 frames are page images belonging to a transaction whose commit frame never
reached the disk — the textbook crash signature. The generator asserts that the
harvested tail contains exactly one commit frame and that it is the last one,
so the removal cannot silently do the wrong thing.

Because the field is part of the first 8 bytes of the frame header, and those
bytes feed the checksum, the frame's checksum is recomputed — see below. The
tail is therefore fully checksum-valid and salt-valid, and still uncommitted.

T4 is deliberately not append-only: it inserts, updates, voids and deletes, and
its `entry_uuid` values are content-free SHA-256 derivatives identical in shape
to every other row's. There is no textual marker distinguishing tail rows, and
no "delete the newest rows" rule recovers the correct state.

## 5. Checksums and determinism

SQLite picks the WAL salts randomly, which would make the artifacts differ on
every run. So the whole log is re-emitted by `walkit.build_wal()`: fixed salts
(`salt1=0x5C3A19E7`, `salt2=0xA1447BD2`), fixed checkpoint sequence (1), magic
`0x377F0683`, and **every** frame checksum recomputed as a rolling chain seeded
by the header checksum. Nothing is faked or described-but-not-computed: the
generator re-parses the assembled file and asserts the chain validates over
exactly the intended number of frames.

Note the magic: `...83` means the checksum words are read **big-endian**, which
is not the native order of the x86-64 build used here (the report records
`host_wal_checksum_big_endian_native: false`). A solver assuming native order
gets nowhere.

## 6. Traps appended after the tail

* **Superseded frames, same salts (6 frames, slots 255..260).** Byte copies of
  live frame slots 140..145, which include T2's commit frame. Their stored
  checksums belong to their *original* positions, so the rolling chain rejects
  them, but their salts match and one carries a non-zero database size.
  Physically this is what a rolled-back or superseded write leaves in the log
  when the machine dies mid-rewrite; it is exactly why the checksum is a
  position-dependent chain instead of a per-frame digest. A solver who filters
  on salts and scans for the last commit marker lands here and produces a
  malformed database (`negative_salt_only_scan.py` demonstrates it).
* **Older-generation frames (14 frames, slots 261..274).** A separately built
  WAL from another database of the same page size, re-chained under different
  salts (`0x1D77F204` / `0x93B0C5AE`). A WAL restart does not truncate the file,
  so leftovers like these are normal. Rejected on salt mismatch.

## 7. WAL header damage

Bytes 0..23 (magic, format version, page size, checkpoint sequence, both salts)
are overwritten with seeded pseudo-random bytes; bytes 24..31 — the header's own
checksum words — are left intact. Consequences:

* SQLite's WAL open path fails the magic/version check and treats the log as
  empty, so no automatic recovery happens and no shortcut exists;
* the page size must come from the main database header;
* the salts must come from the frame headers;
* the frame chain's seed is still recoverable, because the surviving checksum
  words pin down the original header: the only unknowns are the magic (two
  candidates) and the checkpoint sequence number (small). The oracle searches
  and finds magic `0x377F0683`, sequence `1`.

## 8. Main-database page damage

All damaged pages have a valid committed replacement in the log, so the task
stays solvable; the damage only punishes answers that ignore the log.
Targets are chosen programmatically from `dbstat` page types and the log's
last-writer map, never hard-coded:

| page | type | last committed version | damage |
| --- | --- | --- | --- |
| 178 | table leaf (`postings`) | T1 | whole page overwritten |
| 4 | interior b-tree (`journal_entries`) | T3 | whole page overwritten |
| 40 | table leaf (`journal_entries`) | T2 | 64 bytes rewritten at offset 1400 |
| 154 | overflow (`journal_entries`) | T2 | whole page overwritten |

## 9. Self-checks the generator refuses to finish without

* the snapshot log and the T4 log both validate end to end, and the T4 log is a
  strict extension of the snapshot log (asserted frame by frame);
* the snapshot log holds exactly 3 commit frames and ends on one;
* the harvested tail holds exactly one commit frame, in last position;
* the assembled artifact log validates over exactly 255 frames — no more, no
  fewer — and the first frame past that point shares the live salts and the
  region exposes a commit marker;
* the damaged `ledger.db` alone does **not** pass `integrity_check`
  (it reports `database disk image is malformed`);
* handing SQLite the damaged pair recovers nothing
  (`wal_checkpoint(PASSIVE)` reports 0 frames in the log, still malformed);
* replaying every valid frame yields a database that **does** pass
  `integrity_check` and `foreign_key_check` yet differs from the golden state in
  all four user tables;
* at least six of the fixture's transaction-boundary assertions separate the
  golden state from the replay-everything state.

## 10. Recorded values (SQLite 3.45.1, seed 20260819)

```
ledger.db      sha256 312afc58cd4a872b4d6aa62d9393c58cdbaa33d8f5620bb459a5edf444562f92
ledger.db-wal  sha256 83be7a3da42b7b047e73abfa1641a8f9aa7e0a5d280293ac47e612ed2304ee16
golden.db      sha256 1ba64cae9311fdc712f5a6efd4c5067d0dc2d953eed12f3040da2c7956e2ffc3
```

Repairing the header (which the surviving checksum words make possible) and
letting SQLite recover is a legitimate solver route:
`PRAGMA wal_checkpoint(PASSIVE)` then reports `(0, 201, 201)` — SQLite itself
stops at the last commit frame and drops the 54-frame tail, which independently
confirms that the intended answer is the one SQLite's own recovery would reach.
`build/negatives/alt_repair_header.py` exercises it and passes the verifier.

`build/internal/generation_report.json` carries the full machine-readable record,
including the crash-tail entry ids, voided ids, deleted ids and frozen accounts.
