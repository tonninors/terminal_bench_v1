#!/usr/bin/env python3
"""Negative R - read t_xmax as a transaction id even when it is a MultiXactId.

The deepest V4 trap.  HEAP_XMAX_IS_MULTI (0x1000) flips the meaning of the
t_xmax field: it holds a MultiXactId, and in a fresh cluster those are tiny
integers that collide numerically with committed bootstrap transaction ids.
A solver that never tests the bit looks the mxid up in pg_xact, finds
COMMITTED, finds it far below snapshot_xmin, and silently deletes rows that
are merely locked - or locked-plus-updated by a transaction whose fate says
the opposite."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as C            # noqa: E402

G = C.G


def resolve(t, snap, log):
    if t.infomask & 0x0800 or not t.xmax:
        return None
    if G.xmax_is_locked_only(t.infomask) and not t.infomask & 0x1000:
        return None
    return t.xmax                    # the IS_MULTI bit is never consulted


def main() -> int:
    args = C.standard_args(__doc__).parse_args()
    js, attrs, snap, log, tuples = C.load(args)
    rows = []
    for t in tuples:
        if (t.infomask & 0x0300) != 0x0300:          # not frozen
            if t.infomask & 0x0200:
                continue
            if log(t.xmin) != "committed" or snap.in_progress(t.xmin, log):
                continue
        u = resolve(t, snap, log)
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
