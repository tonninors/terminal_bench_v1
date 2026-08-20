#!/usr/bin/env python3
"""Negative P - decode pg_xact but never open pg_subtrans.

Every other rule is applied correctly, including the commit log and the
snapshot.  The one omission is that a transaction id taken off a tuple is
tested against snapshot_xip as-is.

snapshot_xip holds **top-level** xids only.  A tuple written by a
subtransaction of a transaction that was still running carries a subxid that
reads COMMITTED in the commit log and appears in no xip entry, so this solver
concludes the work was finished and visible.  It was not: the subxid's topmost
parent was in flight when the snapshot was taken."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as C            # noqa: E402


HEAP_XMIN_FROZEN = 0x0300
HEAP_XMIN_INVALID = 0x0200
HEAP_XMAX_INVALID = 0x0800


def main() -> int:
    args = C.standard_args(__doc__).parse_args()
    js, attrs, snap, log, tuples = C.load(args)
    rows = []
    for t in tuples:
        if (t.infomask & HEAP_XMIN_FROZEN) != HEAP_XMIN_FROZEN:
            if t.infomask & HEAP_XMIN_INVALID:
                continue
            if log(t.xmin) != "committed":
                continue
            if snap.in_progress(t.xmin):        # log deliberately not passed
                continue
        if not (t.infomask & HEAP_XMAX_INVALID) and t.xmax \
                and not C.G.xmax_is_locked_only(t.infomask):
            if log(t.xmax) == "committed" and not snap.in_progress(t.xmax):
                continue
        rows.append(t.values)
    pk = C.pk_positions(js, attrs)
    rows.sort(key=lambda v: tuple(str(v[i]) for i in pk))
    C.write_csv(args.out, attrs, rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
