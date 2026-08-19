#!/usr/bin/env python3
"""NEGATIVE 2 - replay every structurally valid WAL frame, commit marker or not.

This reproduces the single most attractive wrong answer: the whole surviving
frame chain is checksum-valid, so a solver that stops at "the checksums are
fine" applies the crash tail as well and lands one transaction too far.
"""
from __future__ import annotations
import argparse, sqlite3, struct
from pathlib import Path

WAL_HDR, FRAME_HDR = 32, 24

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

db = bytearray(Path(a.db).read_bytes())
wal = Path(a.wal).read_bytes()
ps = struct.unpack_from(">H", db, 16)[0]
ps = 65536 if ps == 1 else ps
fsz = FRAME_HDR + ps

frames = []
off = WAL_HDR
while off + fsz <= len(wal):
    frames.append(struct.unpack_from(">IIIIII", wal, off) + (wal[off+FRAME_HDR:off+fsz],))
    off += fsz

salt1, salt2 = frames[0][2], frames[0][3]
best = None
for be in (False, True):
    s0, s1 = frames[0][4], frames[0][5]
    n = 1
    for f in frames[1:]:
        if f[2] != salt1 or f[3] != salt2:
            break
        n0, n1 = rolling(struct.pack(">II", f[0], f[1]), s0, s1, be)
        n0, n1 = rolling(f[6], n0, n1, be)
        if (n0, n1) != (f[4], f[5]):
            break
        s0, s1 = n0, n1
        n += 1
    if best is None or n > best[0]:
        best = (n, be)
nvalid = best[0]

applied = {}
for f in frames[:nvalid]:
    applied[f[0]] = f[6]
npages = max(applied)                       # "the log says the file is this big"
need = npages * ps
if len(db) < need:
    db.extend(b"\x00" * (need - len(db)))
for pgno, data in sorted(applied.items()):
    db[(pgno-1)*ps: pgno*ps] = data
del db[need:]
db[18] = db[19] = 1
struct.pack_into(">I", db, 28, npages)
Path(a.out).write_bytes(bytes(db))

c = sqlite3.connect(a.out)
print("frames replayed:", nvalid, "integrity:",
      [r[0] for r in c.execute("PRAGMA integrity_check")][:2],
      "fk:", len(c.execute("PRAGMA foreign_key_check").fetchall()))
c.close()
for side in ("-wal", "-shm"):
    p = Path(a.out + side)
    if p.exists():
        p.unlink()
