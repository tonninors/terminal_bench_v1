#!/usr/bin/env python3
"""Negative V - the MultiXact updater's commit bit is final.

Members, flags and pg_subtrans are all handled correctly, but the snapshot is
never consulted for the update member: committed in pg_xact means the row is
gone.  Updaters that committed after the snapshot was taken - including every
one listed in snapshot_xip - therefore delete rows the snapshot still sees."""
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
        if (t.infomask & 0x0300) != 0x0300:
            if t.infomask & 0x0200:
                continue
            if log(t.xmin) != "committed" or snap.in_progress(t.xmin, log):
                continue
        u = C.deleting_xid(t, log)
        if u is not None:
            if t.infomask & 0x1000:
                if log(u) == "committed":            # snapshot never consulted
                    continue
            elif log(u) == "committed" and not snap.in_progress(u, log):
                continue
        rows.append(t.values)
    pk = C.pk_positions(js, attrs)
    rows.sort(key=lambda v: tuple(str(v[i]) for i in pk))
    C.write_csv(args.out, attrs, rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
