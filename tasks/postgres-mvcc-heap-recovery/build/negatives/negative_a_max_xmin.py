#!/usr/bin/env python3
"""Negative A - pick the physically newest version of each primary key.

Keeps, for every primary key, the tuple with the greatest xmin (breaking ties
by block/offset).  This is the classic 'latest wins' shortcut: it ignores the
snapshot entirely, so every version written by a transaction that committed
after the snapshot was taken wins over the version that was actually visible."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as C            # noqa: E402


def main() -> int:
    args = C.standard_args(__doc__).parse_args()
    js, attrs, snap, status, tuples = C.load(args)
    pk = C.pk_positions(js, attrs)
    best = {}
    for t in tuples:
        key = tuple(t.values[i] for i in pk)
        cur = best.get(key)
        if cur is None or (t.xmin, t.block, t.lp) > (cur.xmin, cur.block, cur.lp):
            best[key] = t
    C.write_csv(args.out, attrs, [best[k].values for k in sorted(best)])
    return 0


if __name__ == "__main__":
    sys.exit(main())
