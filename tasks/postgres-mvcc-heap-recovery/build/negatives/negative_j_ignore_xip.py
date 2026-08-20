#!/usr/bin/env python3
"""Negative J - use the commit log but ignore snapshot_xip.

Applies xmin < snapshot_xmax as the whole visibility rule, so the transaction
that was still running when the snapshot was taken - and only committed
afterwards - is wrongly treated as visible."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as C            # noqa: E402


def main() -> int:
    args = C.standard_args(__doc__).parse_args()
    js, attrs, snap, status, tuples = C.load(args)

    def running(xid):
        return xid >= snap.xmax          # snapshot_xip deliberately not consulted

    rows = []
    for t in tuples:
        if status(t.xmin) != "committed" or running(t.xmin):
            continue
        if t.xmax and status(t.xmax) == "committed" and not running(t.xmax):
            continue
        rows.append(t.values)
    pk = C.pk_positions(js, attrs)
    rows.sort(key=lambda v: tuple(str(v[i]) for i in pk))
    C.write_csv(args.out, attrs, rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
