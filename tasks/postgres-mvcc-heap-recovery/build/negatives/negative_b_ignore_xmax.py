#!/usr/bin/env python3
"""Negative B - keep every tuple whose inserting transaction committed.

Applies the snapshot correctly to xmin but never looks at xmax, so deleted
rows come back and superseded versions are emitted alongside their successors."""
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
        if status(t.xmin) != "committed":
            continue
        if snap.in_progress(t.xmin, status):
            continue
        rows.append(t.values)
    pk = C.pk_positions(js, attrs)
    rows.sort(key=lambda v: tuple(str(v[i]) for i in pk))
    C.write_csv(args.out, attrs, rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
