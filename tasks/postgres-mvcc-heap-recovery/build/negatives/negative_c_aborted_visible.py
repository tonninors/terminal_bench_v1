#!/usr/bin/env python3
"""Negative C - treat aborted transactions as if they had committed.

Every other rule is applied correctly, but a rolled-back UPDATE leaves both a
new tuple (xmin aborted) and an old tuple stamped with an aborted xmax; taking
the aborted work at face value swaps the surviving version for the discarded
one and drops rows whose deletion was rolled back."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as C            # noqa: E402


def main() -> int:
    args = C.standard_args(__doc__).parse_args()
    js, attrs, snap, status, tuples = C.load(args)
    rows = []
    for t in tuples:
        st_min = status(t.xmin)
        if st_min == "in_progress":
            continue
        if snap.in_progress(t.xmin, status):
            continue
        if t.xmax:
            st_max = status(t.xmax)
            if st_max != "in_progress" and not snap.in_progress(t.xmax, status):
                continue
        rows.append(t.values)
    pk = C.pk_positions(js, attrs)
    rows.sort(key=lambda v: tuple(str(v[i]) for i in pk))
    C.write_csv(args.out, attrs, rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
