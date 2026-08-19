#!/usr/bin/env python3
"""NEGATIVE 1 - ignore the WAL and salvage whatever the main database still yields.

A realistic wrong approach: treat ledger.db as the source of truth, read the
schema out of page 1, and copy across every row that can still be read.  The
result is a structurally clean SQLite database holding the *checkpointed base*
state, missing every change that only ever reached the log.
"""
from __future__ import annotations
import argparse, shutil, sqlite3, tempfile
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--db", default="/app/ledger.db")
ap.add_argument("--out", default="/app/recovered.db")
args = ap.parse_args()

tmp = Path(tempfile.mkdtemp()) / "salvage.db"
blob = bytearray(Path(args.db).read_bytes())
blob[18] = blob[19] = 1                      # ignore the log entirely
tmp.write_bytes(bytes(blob))

src = sqlite3.connect(str(tmp))
schema = src.execute(
    "SELECT type, name, sql FROM sqlite_schema WHERE sql IS NOT NULL "
    "AND name NOT LIKE 'sqlite_%'").fetchall()

out = Path(args.out)
if out.exists():
    out.unlink()
for side in ("-wal", "-shm"):
    p = Path(str(out) + side)
    if p.exists():
        p.unlink()
dst = sqlite3.connect(str(out), isolation_level=None)
order = {"table": 0, "index": 1, "view": 2, "trigger": 3}
schema.sort(key=lambda r: order.get(r[0], 9))

tables = [r[1] for r in schema if r[0] == "table"]
for typ, name, sql in schema:
    if typ in ("table", "index", "view"):
        dst.execute(sql)

recovered, lost = 0, 0
for t in tables:
    cols = [r[1] for r in src.execute(f'PRAGMA table_info("{t}")')]
    ph = ",".join("?" * len(cols))
    dst.execute("BEGIN")
    try:
        cur = src.execute(f'SELECT {",".join(chr(34)+c+chr(34) for c in cols)} FROM "{t}"')
        while True:
            try:
                row = cur.fetchone()
            except sqlite3.DatabaseError:
                lost += 1
                break
            if row is None:
                break
            try:
                dst.execute(f'INSERT INTO "{t}" VALUES ({ph})', row)
                recovered += 1
            except sqlite3.DatabaseError:
                lost += 1
    except sqlite3.DatabaseError:
        lost += 1
    dst.execute("COMMIT")

for typ, name, sql in schema:
    if typ == "trigger":
        dst.execute(sql)
dst.close()
src.close()
print(f"salvaged {recovered} rows from the main database, {lost} read failure(s)")
