#!/usr/bin/env python3
"""Negative U - resolve the MultiXact updater without pg_subtrans.

Everything about the multi is decoded correctly, and the updater's commit
state is read from pg_xact - but the snapshot test uses the updater's own xid
without resolving it to its topmost parent.  An updater that ran inside a
SAVEPOINT of a still-in-flight transaction reads COMMITTED and matches no
snapshot_xip entry, so its deletion is wrongly honoured."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as C            # noqa: E402

G = C.G


def resolve(t, snap, log):
    return C.deleting_xid(t, log)


def main() -> int:
    args = C.standard_args(__doc__).parse_args()
    js, attrs, snap, log, tuples = C.load(args)
    rows = []
    for t in tuples:
        if (t.infomask & 0x0300) != 0x0300:
            if t.infomask & 0x0200:
                continue
            if log(t.xmin) != "committed" or snap.in_progress(t.xmin, log):
                continue
        u = resolve(t, snap, log)
        if u is not None and log(u) == "committed" \
                and not snap.in_progress(u):         # log deliberately absent
            continue
        rows.append(t.values)
    pk = C.pk_positions(js, attrs)
    rows.sort(key=lambda v: tuple(str(v[i]) for i in pk))
    C.write_csv(args.out, attrs, rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
