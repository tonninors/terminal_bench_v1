#!/usr/bin/env python3
"""Negative 3 - trust only the surviving pg_xact and drop unresolved tuples.

Every tuple whose xmin has no recorded outcome is discarded as unusable
evidence; tuples whose xmax is unresolved are kept as if undeleted.  This
throws away committed rows and resurrects deleted ones at the same time."""
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

    def outcome(xid):
        st = ev.recorded.get(xid)
        if st is not None:
            return st
        return None

    vis = []
    for t in out_rel.tuples:
        if t.xmin in ev.unknown:
            continue                      # unresolved insert: discarded
        if G.tuple_visible(t, snap, log, outcome):
            vis.append(t)
    C.emit(args, out_rel, vis)
    return 0


if __name__ == "__main__":
    sys.exit(main())
