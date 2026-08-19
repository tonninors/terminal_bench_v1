#!/usr/bin/env python3
"""NEGATIVE 5 - not a SQLite database at all."""
import argparse, random
from pathlib import Path
ap = argparse.ArgumentParser()
ap.add_argument("--out", default="/app/recovered.db")
a = ap.parse_args()
rng = random.Random(4242)
Path(a.out).write_bytes(bytes(rng.randrange(256) for _ in range(64 * 1024)))
print("wrote 64 KiB of noise")
