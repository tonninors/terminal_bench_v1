#!/usr/bin/env python3
"""Negative L - a committed xmax means the row was deleted.

Applies the snapshot correctly to xmin and consults tx_status.csv for xmax,
but reads a non-zero xmax naming a committed transaction as a deletion.  That
is the rule most summaries of MVCC state, and it is wrong for every row whose
xmax records a lock rather than a delete: SELECT ... FOR UPDATE, FOR NO KEY
UPDATE, FOR SHARE and FOR KEY SHARE all park the locker's xid in xmax and set
HEAP_XMAX_LOCK_ONLY, which this solver never looks at."""
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
        if status(t.xmin) != "committed" or snap.in_progress(t.xmin):
            continue
        if t.xmax and status(t.xmax) == "committed":
            continue                      # infomask never consulted
        rows.append(t.values)
    pk = C.pk_positions(js, attrs)
    rows.sort(key=lambda v: tuple(str(v[i]) for i in pk))
    C.write_csv(args.out, attrs, rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
