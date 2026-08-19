#!/usr/bin/env python3
"""NEGATIVE 6 - trust the frame headers, skip the rolling checksum.

Scans every frame whose salts match the head of the log, takes the last one
carrying a commit marker and replays up to it.  The WAL still holds frames this
same generation wrote earlier and later superseded, so the frame headers alone
point at a commit that never happened in that position.
"""
from __future__ import annotations
import argparse, sqlite3, struct
from pathlib import Path

WAL_HDR, FRAME_HDR = 32, 24
ap = argparse.ArgumentParser()
ap.add_argument("--db", default="/app/ledger.db")
ap.add_argument("--wal", default="/app/ledger.db-wal")
ap.add_argument("--out", default="/app/recovered.db")
a = ap.parse_args()

db = bytearray(Path(a.db).read_bytes())
wal = Path(a.wal).read_bytes()
ps = struct.unpack_from(">H", db, 16)[0]
ps = 65536 if ps == 1 else ps
fsz = FRAME_HDR + ps

frames, off = [], WAL_HDR
while off + fsz <= len(wal):
    frames.append(struct.unpack_from(">IIIIII", wal, off) + (wal[off+FRAME_HDR:off+fsz],))
    off += fsz

salt1, salt2 = frames[0][2], frames[0][3]
keep = []
for f in frames:
    if f[2] != salt1 or f[3] != salt2:
        break
    keep.append(f)
commits = [i for i, f in enumerate(keep) if f[1] != 0]
last = commits[-1]
npages = keep[last][1]

applied = {}
for f in keep[: last + 1]:
    applied[f[0]] = f[6]
need = npages * ps
if len(db) < need:
    db.extend(b"\x00" * (need - len(db)))
for pgno, data in sorted(applied.items()):
    if pgno <= npages:
        db[(pgno-1)*ps: pgno*ps] = data
del db[need:]
db[18] = db[19] = 1
struct.pack_into(">I", db, 28, npages)
Path(a.out).write_bytes(bytes(db))

c = sqlite3.connect(a.out)
try:
    ic = [r[0] for r in c.execute("PRAGMA integrity_check")][:2]
except sqlite3.DatabaseError as e:
    ic = [f"error: {e}"]
c.close()
for side in ("-wal", "-shm"):
    p = Path(a.out + side)
    if p.exists():
        p.unlink()
print("salt-matching frames:", len(keep), "chosen commit frame:", last, "integrity:", ic)
