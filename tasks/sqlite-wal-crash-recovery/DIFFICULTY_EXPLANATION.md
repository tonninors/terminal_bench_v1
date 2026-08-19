# Difficulty

## Every input file is self-created

Both solver-facing artifacts are produced programmatically for this benchmark by
`build/generate_case.py` from a fixed seed (20260819). Nothing is downloaded,
scraped, copied from a publication, sample database, tutorial, bug report or any
other external source. The schema is written for this task; account names,
currencies, narratives, memo text and BLOB payloads are synthesised from
self-defined syllable and word tables and a seeded PRNG, so no protected or
externally sourced expression is present. The generator is deterministic:
re-running it reproduces `ledger.db`, `ledger.db-wal` and the golden database
byte for byte, and this is asserted during validation.

## Where the difficulty comes from

The whole task is SQLite storage-engine reasoning. There is no puzzle, no
trivia, no hidden convention to guess, and nothing to look up that is not in
the SQLite file-format documentation.

1. **Automatic recovery is off the table, and the solver has to notice.**
   Opening the pair reports `database disk image is malformed` and
   `PRAGMA wal_checkpoint(TRUNCATE)` returns `(0, 0, 0)`. SQLite has silently
   decided the log is empty and, on close, deletes the `-wal`. Anyone who does
   not understand *why* will conclude the log is worthless — and may destroy it.

2. **The log has to be parsed from the outside.** With the WAL header's
   descriptive fields gone, the page size must come from the main database
   header, the frame stride from the page size, and the generation's salts from
   the frame headers.

3. **Checksum byte order is not the host's.** The artifact's checksum words are
   big-endian. On a little-endian host a native-order implementation validates
   nothing past the first frame. The solver must know that the WAL magic
   selects the interpretation, and must either re-derive the destroyed header —
   its two checksum words survive, and everything else in it is known or a small
   integer, so it can be recovered by search — or determine the order
   empirically from the body.

4. **The rolling checksum is genuinely load-bearing.** Past the crash tail the
   log still holds frames this same generation wrote earlier and later
   superseded, carrying the *correct* salts and one *valid-looking commit
   marker*. Salt filtering plus commit-marker scanning walks straight into them
   and produces a malformed database. Only the position-dependent chain rejects
   them. Further out sit frames from an older generation with different salts —
   a second, cruder trap.

5. **Structural validity is not the answer.** The most attractive wrong answer
   — replay every checksum-valid frame — yields a database that passes
   `PRAGMA integrity_check` and `PRAGMA foreign_key_check` and looks entirely
   healthy. It is wrong because the last 54 valid frames carry no commit marker:
   they are a transaction whose commit never reached the disk. Getting this
   right requires understanding that a WAL commit is the *commit frame*, not the
   presence of page images. This is the core of the task, and it is exactly the
   distinction the verifier tests.

6. **Recovery is not "the last transaction".** Four pages of the main database
   are damaged, and their good committed versions sit in *different*
   transactions of the log — one in the first, one in the second, one in the
   third, plus a wrecked overflow page. Correct output requires replaying the
   whole committed prefix in order and keeping the last image of each page at or
   before the commit boundary, not just the final transaction's frames.

7. **File-level semantics have to be respected.** The commit frame's
   database-size field (382 pages) is authoritative, while the main file is 323
   pages; the output must be sized from the commit, not from the input file.
   The result must also stand on its own, so the header's journal-mode bytes
   have to be dealt with.

8. **Undoing the wrong answer by hand is not available.** The crash tail both
   inserts rows and modifies and deletes existing ones, and the identifiers it
   writes are content-free hashes that look exactly like every other row. There
   is no marker to grep for and no "drop the newest N rows" rule that restores
   the correct state. The commit boundary has to be found in the log.

## What the difficulty is *not*

* **Not volume.** The database is ~1.3 MB, the log ~1.1 MB, 275 frame slots,
  four tables, 1884 rows. Everything fits in memory and runs in milliseconds.
* **Not obscure lookup.** Every fact needed is in the published SQLite file
  format documentation: the WAL header layout, the frame header layout, the
  checksum algorithm, and the meaning of the database-size field.
* **Not formatting.** The deliverable is one SQLite file. No output format, no
  report, no naming scheme beyond the path.
* **Not artificial limits.** No resource caps, no forbidden tools, no timing
  requirements. `sqlite3` and Python are available in the image.
* **Not a puzzle.** Nothing is encoded, hidden or riddled. The artifacts are
  what a real crash of a WAL-mode SQLite service plus a damaged first sector
  would leave behind.

## Expected effort

A qualified database or storage engineer who knows the SQLite file format
should reach a correct answer in roughly half a day: an hour or two to
establish that SQLite refuses the log and to parse the frames, an hour or two on
the checksum byte order and the header reconstruction, then the commit-boundary
reasoning and the file reconstruction. Someone who has never read the WAL format
will spend most of a day, largely in the documentation. The verifier accepts
any implementation that lands on the right logical state — page replay, a SQL
rebuild and `VACUUM INTO` are all tested and all pass.
