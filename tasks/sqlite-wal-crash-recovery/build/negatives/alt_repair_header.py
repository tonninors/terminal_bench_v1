#!/usr/bin/env python3
"""ALTERNATE (legitimate) construction - rebuild the destroyed WAL header and
let SQLite perform the recovery itself.

The header's two checksum words survive, and everything they cover is either
known (format version, page size, salts) or a small integer (magic, checkpoint
sequence), so the original header can be re-derived by search.  Once it is back,
SQLite's own WAL recovery stops at the last valid commit frame, discards the
crash tail and rejects the superseded frames, which is exactly the required
state.  Included to show the verifier scores the outcome, not a technique.
"""
from __future__ import annotations
import argparse, shutil, sqlite3, struct, tempfile
from pathlib import Path

WAL_HDR, FRAME_HDR, VERSION = 32, 24, 3007000

def rolling(data, s0, s1, be):
    fmt = ">II" if be else "<II"
    for off in range(0, len(data), 8):
        x0, x1 = struct.unpack_from(fmt, data, off)
        s0 = (s0 + x0 + s1) & 0xFFFFFFFF
        s1 = (s1 + x1 + s0) & 0xFFFFFFFF
    return s0, s1

ap = argparse.ArgumentParser()
ap.add_argument("--db", default="/app/ledger.db")
ap.add_argument("--wal", default="/app/ledger.db-wal")
ap.add_argument("--out", default="/app/recovered.db")
a = ap.parse_args()

db = Path(a.db).read_bytes()
wal = bytearray(Path(a.wal).read_bytes())
ps = struct.unpack_from(">H", db, 16)[0]
ps = 65536 if ps == 1 else ps
salt1, salt2 = struct.unpack_from(">II", wal, WAL_HDR + 8)
want = struct.unpack_from(">II", wal, 24)

found = None
for magic in (0x377F0682, 0x377F0683):
    for seq in range(1 << 17):
        head = struct.pack(">IIIIII", magic, VERSION, ps, seq, salt1, salt2)
        if rolling(head, 0, 0, bool(magic & 1)) == want:
            found = (magic, seq, head)
            break
    if found:
        break
if not found:
    raise SystemExit("could not re-derive the WAL header")
magic, seq, head = found
wal[0:24] = head
print(f"repaired WAL header: magic=0x{magic:08X} ckpt_seq={seq} page_size={ps}")

work = Path(tempfile.mkdtemp())
(work / "ledger.db").write_bytes(db)
(work / "ledger.db-wal").write_bytes(bytes(wal))
c = sqlite3.connect(str(work / "ledger.db"), isolation_level=None)
print("wal_checkpoint:", c.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone())
c.execute("PRAGMA journal_mode=DELETE")
print("integrity:", [r[0] for r in c.execute("PRAGMA integrity_check")][:2])
c.close()
for side in ("-wal", "-shm"):
    p = work / ("ledger.db" + side)
    if p.exists():
        p.unlink()
for side in ("", "-wal", "-shm"):
    p = Path(str(a.out) + side)
    if p.exists():
        p.unlink()
shutil.copyfile(work / "ledger.db", a.out)
print("SQLite recovered the database from the repaired log")
