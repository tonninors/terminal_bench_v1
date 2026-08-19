#!/usr/bin/env python3
"""NEGATIVE 3 - correct rows, but a user-defined schema object is lost.

Models a solver who rebuilds the data by dumping and reloading but forgets the
partial index and one trigger.
"""
import argparse, shutil, sqlite3
from pathlib import Path
ap = argparse.ArgumentParser()
ap.add_argument("--src", required=True, help="a correctly recovered database")
ap.add_argument("--out", default="/app/recovered.db")
a = ap.parse_args()
shutil.copyfile(a.src, a.out)
c = sqlite3.connect(a.out, isolation_level=None)
c.execute("DROP INDEX ix_entries_open_work")
c.execute("DROP TRIGGER trg_accounts_status_au")
c.execute("VACUUM")
c.close()
for side in ("-wal", "-shm"):
    p = Path(a.out + side)
    if p.exists():
        p.unlink()
print("dropped one partial index and one trigger")
