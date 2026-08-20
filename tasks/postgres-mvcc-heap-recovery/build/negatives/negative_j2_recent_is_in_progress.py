#!/usr/bin/env python3
"""Negative J2 - treat every recent transaction as still running.

Uses snapshot_xmin and snapshot_xmax as a plain interval and never reads
snapshot_xip, so any transaction at or above snapshot_xmin counts as in
progress.  Several transactions committed *between* the writers that this
snapshot lists, so their xids fall inside that interval while being absent
from xip - their work is visible, and this solver discards it."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as C            # noqa: E402


def main() -> int:
    args = C.standard_args(__doc__).parse_args()
    js, attrs, snap, status, tuples = C.load(args)

    def running(xid):
        return xid >= snap.xmin           # the xip list is never consulted

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
