#!/usr/bin/env python3
"""Negative Y - test HEAP_XMIN_INVALID before the frozen mask.

HEAP_XMIN_FROZEN is the COMBINATION of HEAP_XMIN_COMMITTED and
HEAP_XMIN_INVALID: freezing sets both bits and keeps the raw xmin.  A solver
that checks the INVALID bit on its own reads every frozen tuple as 'inserter
aborted' and throws away the entire frozen base population."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as C            # noqa: E402

G = C.G


def main() -> int:
    args = C.standard_args(__doc__).parse_args()
    js, attrs, snap, log, tuples = C.load(args)
    rows = []
    for t in tuples:
        if t.infomask & 0x0200:                      # tested before FROZEN
            continue
        if not (t.infomask & 0x0100):
            if log(t.xmin) != "committed" or snap.in_progress(t.xmin, log):
                continue
        elif snap.in_progress(t.xmin, log):
            continue
        u = C.deleting_xid(t, log)
        if u is not None and log(u) == "committed" \
                and not snap.in_progress(u, log):
            continue
        rows.append(t.values)
    pk = C.pk_positions(js, attrs)
    rows.sort(key=lambda v: tuple(str(v[i]) for i in pk))
    C.write_csv(args.out, attrs, rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
