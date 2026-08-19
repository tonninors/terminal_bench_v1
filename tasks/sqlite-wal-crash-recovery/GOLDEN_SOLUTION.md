# Golden solution

Reference implementation: `solution/golden_recover.py` (invoked by `solution.sh`).
It reads **only** `/app/ledger.db` and `/app/ledger.db-wal`; no hidden expected
database is consulted anywhere in it, and no recovered row is hard-coded.

The steps below are what a qualified database/storage engineer would do. Any
implementation that reaches the same logical state passes the verifier.

---

## 0. Establish what SQLite itself will and will not do

Copy the pair somewhere scratch and open it. SQLite reports
`database disk image is malformed`, and `PRAGMA wal_checkpoint(TRUNCATE)`
returns `(0, 0, 0)` — zero frames in the log. SQLite validated the WAL header,
found it unusable, and treated the log as empty; then, on close, it deleted the
`-wal` file. Conclusion: automatic recovery is not available, the log must be
replayed by hand, and the artifacts must be treated as read-only evidence.

## 1. Page size

The WAL header's page-size field is destroyed, so take the page size from the
main database header instead: big-endian `uint16` at offset 16, where the value
`1` means 65536. Here it is **4096**.

A WAL frame is a 24-byte frame header followed by one page image, so the frame
stride is `24 + 4096 = 4120` and frame *i* starts at `32 + i * 4120`.
`/app/ledger.db-wal` holds **275** complete frame slots.

## 2. Frame header layout

Every WAL integer is big-endian:

| offset | field |
| --- | --- |
| 0 | page number |
| 4 | database size in pages **after** this frame — non-zero only on a commit frame |
| 8 | salt-1 (copied from the WAL header) |
| 12 | salt-2 (copied from the WAL header) |
| 16 | checksum-1 |
| 20 | checksum-2 |

The salts survive in every frame, so the generation's salts can be read
straight out of frame 0: `salt1 = 0x5C3A19E7`, `salt2 = 0xA1447BD2`.

## 3. Rebuild the destroyed WAL header

The first 24 bytes of the header are garbage, but the two checksum words at
offsets 24..31 survive, and the header checksum is computed over exactly those
first 24 bytes seeded with `(0, 0)`. Every field in them is either known or
tiny:

* magic — `0x377F0682` (checksum words read little-endian) or `0x377F0683`
  (big-endian);
* file format version — `3007000`;
* page size — 4096, from step 1;
* checkpoint sequence number — a small counter;
* salt-1, salt-2 — from step 2.

Two candidate magics times a short scan over the checkpoint counter recovers
the header exactly: magic `0x377F0683`, checkpoint sequence `1`. Two facts fall
out of this, and both matter:

* the surviving header checksum words are the **seed** of the frame chain, so
  even frame 0 can be authenticated;
* the checksum words are interpreted **big-endian**, which is *not* the native
  order of a typical x86-64 host. An implementation that assumes native order
  validates nothing beyond the first frame.

If you prefer not to reconstruct the header, the byte order can also be settled
empirically: frame 0's stored checksum seeds frame 1, so try both orders and
keep whichever validates a long run of frames. That leaves frame 0 itself
unauthenticated, which does not change the answer here.

## 4. The rolling checksum

For each frame, in order, starting from the seed `(s0, s1)`:

```
s0, s1 = cksum(frame_header[0:8], s0, s1)   # page number + database size only
s0, s1 = cksum(page_image,        s0, s1)
```

where `cksum` walks the input in 8-byte steps as two 32-bit words `x0, x1`
(big-endian here) and accumulates

```
s0 = (s0 + x0 + s1) mod 2**32
s1 = (s1 + x1 + s0) mod 2**32
```

A frame is part of the log if and only if its salts match the header **and**
the recomputed pair equals the stored pair. The checksum is a *chain*: it
depends on the frame's position, not just its bytes.

## 5. Walk the chain and find its end

Walking from frame 0 stops after **255** frames. What follows is not noise:

* frames 255..260 carry the *correct* salts and one of them even carries a
  commit marker. They are frames this same generation wrote earlier and later
  superseded; at their current position the rolling checksum does not chain, so
  they are not part of the log. Rejecting them requires the checksum — the
  frame headers alone point at a commit that never happened here.
* frames 261..274 are leftovers from an older generation of this log, still
  present because a WAL restart does not truncate the file. Their salts differ.

## 6. Pick the commit

Inside the valid 255-frame chain the commit frames (non-zero database size) are
at indexes **47**, **143** and **200**. Frames 201..254 are checksum-valid but
carry no commit marker at all: that is the crash tail — the page images of a
transaction whose commit frame never reached the disk. Recovery ends at frame
**200**, whose database-size field is **382 pages**.

## 7. Rebuild the database file

* Start from the 323 pages of `/app/ledger.db` and size the output to exactly
  `382 * 4096` bytes — the commit frame's database size is authoritative, not
  the length of the main file.
* Replay frames 0..200 in order, so that for any page written more than once
  the **last** image at or before the commit wins. 168 distinct pages are
  written; several of them replace pages that are damaged in the main file
  (page 4, an interior b-tree page; page 40, subtly altered inside a record;
  pages 154 and 178, an overflow page and a table leaf that were overwritten
  wholesale). The damage never has to be diagnosed: the log holds a committed
  replacement for every damaged page.
* Do **not** apply frames 201..254, and do not apply anything past frame 254.
* Make the result standalone: set the read/write format bytes at offsets 18 and
  19 of page 1 to `1`, so SQLite does not look for a `-wal` sidecar, and make
  sure page 1's database-size field at offset 28 agrees with 382.

## 8. Validate

Open the result on its own and confirm `PRAGMA integrity_check` returns `ok`
and `PRAGMA foreign_key_check` returns nothing. Both hold for the correct
answer. The recovered database contains 64 accounts, 360 journal entries,
720 postings and 740 audit rows.

## Why the obvious shortcuts are wrong

| approach | outcome |
| --- | --- |
| ship `/app/ledger.db` as-is | malformed; four damaged pages |
| let SQLite recover the pair | the log is ignored entirely; still malformed |
| salvage rows from the main database only | clean file, but the base state — three committed transactions are missing |
| replay the whole valid chain | clean file that passes `integrity_check` and `foreign_key_check`, but one transaction too far |
| trust frame headers, skip the checksum | lands on a superseded commit marker; the file is malformed |
