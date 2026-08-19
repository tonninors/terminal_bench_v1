#!/usr/bin/env python3
"""ALTERNATE (legitimate) construction - VACUUM INTO, which relays out every
page but keeps the logical content identical."""
import argparse, sqlite3
from pathlib import Path
ap = argparse.ArgumentParser()
ap.add_argument("--src", required=True)
ap.add_argument("--out", default="/app/recovered.db")
a = ap.parse_args()
for side in ("", "-wal", "-shm"):
    p = Path(str(a.out) + side)
    if p.exists():
        p.unlink()
c = sqlite3.connect(f"file:{a.src}?mode=ro", uri=True)
c.execute("VACUUM INTO ?", (a.out,))
c.close()
print("vacuumed into a fresh file")
