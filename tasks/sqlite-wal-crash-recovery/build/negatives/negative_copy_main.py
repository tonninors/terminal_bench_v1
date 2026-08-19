#!/usr/bin/env python3
"""NEGATIVE 0 - hand back the damaged main database unchanged."""
import argparse, shutil
ap = argparse.ArgumentParser()
ap.add_argument("--db", default="/app/ledger.db")
ap.add_argument("--out", default="/app/recovered.db")
a = ap.parse_args()
shutil.copyfile(a.db, a.out)
print("copied main database verbatim")
