#!/usr/bin/env python3
"""Alternative correct solution - PostgreSQL's own answer, reshaped.

`build/internal/golden.csv` was produced by the server itself, from inside the
repeatable read transaction that owns the target snapshot; it never passed
through the page-level oracle.  This script re-emits exactly those rows with the
order rotated and the booleans spelled TRUE/FALSE, showing that the verifier
accepts any faithful rendering of the same logical state, regardless of how it
was obtained or how it is ordered.

Author-side only: golden.csv is not part of the solver-facing bundle.
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

TASK = Path(__file__).resolve().parent.parent.parent
BOOL_SPELLING = {"t": "TRUE", "f": "FALSE"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=str(TASK / "build" / "internal" / "golden.csv"))
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    with open(a.src, encoding="utf-8", newline="") as fh:
        rows = [r for r in csv.reader(fh) if r]
    header, data = rows[0], rows[1:]
    bool_cols = [i for i, c in enumerate(header) if c == "is_active"]

    reshaped = []
    for row in data:
        row = list(row)
        for i in bool_cols:
            row[i] = BOOL_SPELLING.get(row[i], row[i])
        reshaped.append(row)

    # rotate by a third: a permutation, so no row is added or lost
    cut = len(reshaped) // 3
    rotated = reshaped[cut:] + reshaped[:cut]
    assert sorted(map(tuple, rotated)) == sorted(map(tuple, reshaped))

    with open(a.out, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(header)
        w.writerows(rotated)
    print("re-emitted %d row(s) from the PostgreSQL reference to %s"
          % (len(rotated), a.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
