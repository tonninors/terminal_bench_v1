#!/usr/bin/env python3
"""Negative 5 - resolve transaction fate per TUPLE instead of per XID.

Each tuple is judged only by the hint bits stamped on that tuple itself;
a fact PostgreSQL recorded about the same transaction on another tuple - or
in another relation - is never applied.  One transaction ends up committed
on one page and aborted on another, which no real PostgreSQL history can
produce."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as C            # noqa: E402

G = C.G


def main() -> int:
    args = C.standard_args(__doc__).parse_args()
    js, relations, log, snap, ev = C.load(args)
    out_rel = relations[0]

    def outcome_for(t):
        m = t.infomask

        def fn(xid):
            st = ev.recorded.get(xid)
            if st is not None:
                return st
            if xid == t.xmin:
                if m & G.HEAP_XMIN_COMMITTED:
                    return "committed"
                if m & G.HEAP_XMIN_INVALID:
                    return "aborted"
                return "aborted"          # this tuple carries no fact
            if xid == t.xmax:
                if m & G.HEAP_XMAX_COMMITTED:
                    return "committed"
                if m & G.HEAP_XMAX_INVALID:
                    return "aborted"
                return "aborted"
            return None
        return fn

    vis = [t for t in out_rel.tuples
           if G.tuple_visible(t, snap, log, outcome_for(t))]
    C.emit(args, out_rel, vis)
    return 0


if __name__ == "__main__":
    sys.exit(main())
