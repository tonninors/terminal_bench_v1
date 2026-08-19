#!/bin/bash
# Oracle solution for the sqlite-wal-crash-recovery task.
#
# Reconstructs the database as of the last fully committed, internally valid WAL
# transaction using only the two crash artifacts.  No hidden expected database
# is consulted and no recovered row is hard-coded.
#
# GENERATED FILE - edit solution/golden_recover.py and rerun
# build/make_solution_sh.py instead.
set -euo pipefail

LEDGER_DB=${LEDGER_DB:-/app/ledger.db}
LEDGER_WAL=${LEDGER_WAL:-/app/ledger.db-wal}
RECOVERED_DB=${RECOVERED_DB:-/app/recovered.db}

PROG=$(mktemp /tmp/golden_recover.XXXXXX.py)
trap 'rm -f "$PROG"' EXIT

cat > "$PROG" <<'GOLDEN_RECOVER_EOF'
#!/usr/bin/env python3
"""Reference recovery for a SQLite database whose WAL header has been destroyed.

Reads only the two crash artifacts and reconstructs the database as it existed
at the last fully committed, internally valid WAL transaction.

    golden_recover.py [--db /app/ledger.db] [--wal /app/ledger.db-wal]
                      [--out /app/recovered.db] [--report]

No knowledge of the expected contents is used anywhere in this file.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import struct
import sys
from pathlib import Path

WAL_HDR = 32
FRAME_HDR = 24
WAL_FORMAT_VERSION = 3007000
MAGIC_LE = 0x377F0682   # checksum words interpreted little-endian
MAGIC_BE = 0x377F0683   # checksum words interpreted big-endian


# ---------------------------------------------------------------- checksums
def rolling(data: bytes, s0: int, s1: int, big_endian: bool) -> tuple[int, int]:
    fmt = ">II" if big_endian else "<II"
    for off in range(0, len(data), 8):
        x0, x1 = struct.unpack_from(fmt, data, off)
        s0 = (s0 + x0 + s1) & 0xFFFFFFFF
        s1 = (s1 + x1 + s0) & 0xFFFFFFFF
    return s0, s1


# ---------------------------------------------------------------- db header
def read_page_size(db: bytes) -> int:
    if len(db) < 100:
        raise SystemExit("main database is too small to contain a header")
    raw = struct.unpack_from(">H", db, 16)[0]
    ps = 65536 if raw == 1 else raw
    if ps < 512 or ps > 65536 or (ps & (ps - 1)):
        raise SystemExit(f"implausible page size in main database header: {raw}")
    return ps


# ---------------------------------------------------------------- wal frames
def raw_frames(wal: bytes, page_size: int):
    """Yield (index, offset, pgno, db_size, salt1, salt2, c1, c2, data) for every
    physically complete frame slot in the file.  No validation yet."""
    frame_size = FRAME_HDR + page_size
    idx = 0
    off = WAL_HDR
    while off + frame_size <= len(wal):
        pgno, db_size, s1, s2, c1, c2 = struct.unpack_from(">IIIIII", wal, off)
        yield (idx, off, pgno, db_size, s1, s2, c1, c2,
               wal[off + FRAME_HDR: off + frame_size])
        idx += 1
        off += frame_size


def pick_salts(frames) -> tuple[int, int]:
    """The salts of the generation that owns the head of the log."""
    counts: dict[tuple[int, int], int] = {}
    first = None
    for i, (_idx, _off, _p, _d, s1, s2, _c1, _c2, _data) in enumerate(frames):
        if first is None:
            first = (s1, s2)
        counts[(s1, s2)] = counts.get((s1, s2), 0) + 1
        if i >= 32:
            break
    if first is None:
        raise SystemExit("WAL contains no complete frames")
    return first


def recover_seed(wal: bytes, page_size: int, salt1: int, salt2: int,
                 max_ckpt_seq: int = 1 << 17):
    """The WAL header's own checksum seeds the frame chain.  The descriptive part
    of the header is destroyed, but its two checksum words survive, so the
    original header can be re-derived: everything in it is either known
    (format version, page size, salts) or a small integer (checkpoint sequence,
    magic).  Returns (big_endian, seed0, seed1, magic, ckpt_seq) or None."""
    want0, want1 = struct.unpack_from(">II", wal, 24)
    for magic in (MAGIC_LE, MAGIC_BE):
        be = bool(magic & 1)
        for seq in range(max_ckpt_seq):
            head = struct.pack(">IIIIII", magic, WAL_FORMAT_VERSION, page_size,
                               seq, salt1, salt2)
            c0, c1 = rolling(head, 0, 0, be)
            if c0 == want0 and c1 == want1:
                return be, c0, c1, magic, seq
    return None


def chain_length(frames, salt1: int, salt2: int, seed0: int, seed1: int,
                 big_endian: bool) -> int:
    s0, s1 = seed0, seed1
    n = 0
    for (_idx, _off, pgno, db_size, fs1, fs2, c1, c2, data) in frames:
        if fs1 != salt1 or fs2 != salt2 or pgno == 0:
            break
        n0, n1 = rolling(struct.pack(">II", pgno, db_size), s0, s1, big_endian)
        n0, n1 = rolling(data, n0, n1, big_endian)
        if (n0, n1) != (c1, c2):
            break
        s0, s1 = n0, n1
        n += 1
    return n


def infer_seed_from_body(frames, salt1: int, salt2: int):
    """Fallback when the header cannot be re-derived: frame 1's stored checksum
    is the seed for frame 2, so the *relative* chain can still be validated and
    the checksum byte order determined, at the cost of not authenticating the
    very first frame."""
    best = None
    for be in (False, True):
        if len(frames) < 2:
            break
        seed0, seed1 = frames[0][6], frames[0][7]
        n = chain_length(frames[1:], salt1, salt2, seed0, seed1, be)
        if best is None or n > best[0]:
            best = (n, be, seed0, seed1)
    if best is None or best[0] == 0:
        raise SystemExit("unable to determine WAL checksum byte order")
    return best[1], best[2], best[3]


# ---------------------------------------------------------------- rebuild
def rebuild(db: bytes, page_size: int, applied, db_size_pages: int) -> bytes:
    out = bytearray(db[: db_size_pages * page_size])
    if len(out) < db_size_pages * page_size:
        out.extend(b"\x00" * (db_size_pages * page_size - len(out)))
    for pgno, data in applied:
        if pgno <= db_size_pages:
            out[(pgno - 1) * page_size: pgno * page_size] = data
    # the recovered file must stand alone: switch the header out of WAL mode so
    # SQLite never looks for a -wal sidecar
    out[18] = 1
    out[19] = 1
    # keep the header's "database size in pages" consistent with the commit
    struct.pack_into(">I", out, 28, db_size_pages)
    return bytes(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="/app/ledger.db")
    ap.add_argument("--wal", default="/app/ledger.db-wal")
    ap.add_argument("--out", default="/app/recovered.db")
    ap.add_argument("--report", action="store_true",
                    help="print a JSON description of what was recovered")
    args = ap.parse_args(argv)

    db = Path(args.db).read_bytes()
    wal = Path(args.wal).read_bytes()

    page_size = read_page_size(db)
    frames = list(raw_frames(wal, page_size))
    if not frames:
        raise SystemExit("no complete WAL frames found")

    salt1, salt2 = pick_salts(frames)
    seed = recover_seed(wal, page_size, salt1, salt2)
    if seed is not None:
        big_endian, seed0, seed1, magic, ckpt_seq = seed
        valid = chain_length(frames, salt1, salt2, seed0, seed1, big_endian)
        anchored = True
    else:
        magic = ckpt_seq = None
        big_endian, seed0, seed1 = infer_seed_from_body(frames, salt1, salt2)
        valid = 1 + chain_length(frames[1:], salt1, salt2, seed0, seed1, big_endian)
        anchored = False

    if valid == 0:
        raise SystemExit("no valid WAL frames")

    good = frames[:valid]
    commits = [i for i, f in enumerate(good) if f[3] != 0]
    if not commits:
        raise SystemExit("the WAL contains no committed transaction")
    last = commits[-1]
    db_size_pages = good[last][3]

    # Later frames win: replay in order and keep the final image of every page.
    latest: dict[int, bytes] = {}
    for f in good[: last + 1]:
        latest[f[2]] = f[8]
    if 1 not in latest:
        raise SystemExit("page 1 was never written by the recovered transactions")

    missing = [p for p in range(1, db_size_pages + 1)
               if p not in latest and (p * page_size) > len(db)]
    if missing:
        raise SystemExit(f"pages {missing[:5]} are present in neither artifact")

    out = rebuild(db, page_size, sorted(latest.items()), db_size_pages)
    Path(args.out).write_bytes(out)

    conn = sqlite3.connect(args.out)
    try:
        ic = [r[0] for r in conn.execute("PRAGMA integrity_check")]
        fk = conn.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        conn.close()
    for side in ("-wal", "-shm"):
        p = Path(args.out + side)
        if p.exists():
            p.unlink()
    if ic != ["ok"]:
        raise SystemExit(f"recovered database failed integrity_check: {ic[:5]}")
    if fk:
        raise SystemExit(f"recovered database failed foreign_key_check: {fk[:5]}")

    if args.report:
        print(json.dumps({
            "page_size": page_size,
            "frame_slots_in_file": len(frames),
            "salt1": salt1, "salt2": salt2,
            "header_reconstructed": anchored,
            "magic": magic, "ckpt_seq": ckpt_seq,
            "checksum_big_endian": big_endian,
            "valid_chained_frames": valid,
            "commit_frames_at": commits,
            "recovered_commit_frame": last,
            "discarded_tail_frames": valid - (last + 1),
            "frames_after_valid_chain": len(frames) - valid,
            "db_size_pages": db_size_pages,
            "distinct_pages_applied": len(latest),
            "integrity_check": ic, "foreign_key_check": fk,
        }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
GOLDEN_RECOVER_EOF

python3 "$PROG" --db "$LEDGER_DB" --wal "$LEDGER_WAL" --out "$RECOVERED_DB" --report
