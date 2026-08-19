#!/usr/bin/env python3
"""ALTERNATE (legitimate) construction - rebuild the recovered database through
SQL rather than by copying pages, to prove the verifier rewards the outcome and
not one particular implementation technique."""
import argparse, sqlite3
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--src", required=True)
ap.add_argument("--out", default="/app/recovered.db")
a = ap.parse_args()

out = Path(a.out)
for side in ("", "-wal", "-shm"):
    p = Path(str(out) + side)
    if p.exists():
        p.unlink()

src = sqlite3.connect(f"file:{a.src}?mode=ro", uri=True)
dst = sqlite3.connect(str(out), isolation_level=None)
dst.execute("PRAGMA foreign_keys=OFF")

schema = src.execute(
    "SELECT type, name, sql FROM sqlite_schema WHERE sql IS NOT NULL "
    "AND name NOT LIKE 'sqlite_%'").fetchall()
phase = {"table": 0, "index": 1, "view": 2, "trigger": 3}

dst.execute("BEGIN")
for typ, name, sql in sorted(schema, key=lambda r: phase[r[0]]):
    if typ == "table":
        dst.execute(sql)
for typ, name, sql in schema:
    if typ != "table":
        continue
    cols = [r[1] for r in src.execute(f'PRAGMA table_info("{name}")')]
    sel = ",".join(f'"{c}"' for c in cols)
    ph = ",".join("?" * len(cols))
    rows = src.execute(f'SELECT {sel} FROM "{name}"').fetchall()
    dst.executemany(f'INSERT INTO "{name}" ({sel}) VALUES ({ph})', rows)
for typ, name, sql in sorted(schema, key=lambda r: phase[r[0]]):
    if typ != "table":
        dst.execute(sql)
dst.execute("COMMIT")
dst.execute("VACUUM")
dst.close()
src.close()
for side in ("-wal", "-shm"):
    p = Path(str(out) + side)
    if p.exists():
        p.unlink()
print("rebuilt the database from SQL")
