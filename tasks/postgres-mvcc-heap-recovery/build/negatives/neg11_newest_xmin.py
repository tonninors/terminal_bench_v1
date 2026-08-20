#!/usr/bin/env python3
"""Negative 11 - newest xmin per primary key, no transaction reasoning.

The V1-era shortcut: for every key keep the physically newest version whose
insert is not positively known aborted.  Ignores the snapshot, the missing
outcomes and every constraint."""
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
    best = {}
    for t in out_rel.tuples:
        if ev.recorded.get(t.xmin) == "aborted":
            continue
        k = out_rel.key_of(t)
        cur = best.get(k)
        if cur is None or (t.xmin, t.block, t.lp) > (cur.xmin, cur.block,
                                                     cur.lp):
            best[k] = t
    C.emit(args, out_rel, list(best.values()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
