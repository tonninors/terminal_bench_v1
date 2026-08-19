#!/usr/bin/env python3
"""NEGATIVE 4 - the answer is only correct while its -wal sidecar is present.

The main file holds the over-applied (crash-tail) state; the corrections that
make it right are left uncheckpointed in the log.  Opened as a pair it looks
perfect, but the deliverable is a single standalone file.
"""
import argparse, shutil, sqlite3, tempfile
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--wrong", required=True, help="a logically wrong database to use as the base file")
ap.add_argument("--right", required=True, help="the correct database to graft in via the WAL")
ap.add_argument("--out", default="/app/recovered.db")
a = ap.parse_args()

out = Path(a.out)
for side in ("", "-wal", "-shm"):
    p = Path(str(out) + side)
    if p.exists():
        p.unlink()
shutil.copyfile(a.wrong, out)

c = sqlite3.connect(str(out), isolation_level=None)
c.execute("PRAGMA journal_mode=WAL")
c.execute("PRAGMA wal_autocheckpoint=0")
c.execute("PRAGMA foreign_keys=OFF")
c.execute(f"ATTACH DATABASE '{a.right}' AS good")
triggers = [r[0] for r in c.execute(
    "SELECT sql FROM sqlite_schema WHERE type='trigger'")]
names = [r[0] for r in c.execute(
    "SELECT name FROM sqlite_schema WHERE type='trigger'")]
tables = [r[0] for r in c.execute(
    "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
c.execute("BEGIN")
for n in names:
    c.execute(f"DROP TRIGGER {n}")
for t in tables:
    c.execute(f'DELETE FROM main."{t}"')
    c.execute(f'INSERT INTO main."{t}" SELECT * FROM good."{t}"')
for sql in triggers:
    c.execute(sql)
c.execute("COMMIT")

# copy the pair out *while the connection is still open* so that nothing is
# checkpointed back into the main file
stash = Path(tempfile.mkdtemp())
shutil.copyfile(out, stash / "db")
shutil.copyfile(Path(str(out) + "-wal"), stash / "wal")
c.close()

for side in ("", "-wal", "-shm"):
    p = Path(str(out) + side)
    if p.exists():
        p.unlink()
shutil.copyfile(stash / "db", out)
shutil.copyfile(stash / "wal", Path(str(out) + "-wal"))
print("wrote a database whose correctness depends on its -wal sidecar")
