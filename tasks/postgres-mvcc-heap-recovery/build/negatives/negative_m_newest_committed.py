#!/usr/bin/env python3
"""Negative M - keep the newest committed version of each key.

For every primary key it picks the tuple with the greatest xmin among those
whose inserting transaction committed, and emits it.  No snapshot, no xmax, no
infomask: the tuple header is used only for xmin.  This is the 'just take the
latest good version' shortcut, and it is wrong wherever the newest committed
version was written after the snapshot, or was later deleted, or is one of the
rows whose only surviving version was already removed."""
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
        if status(t.xmin) != "committed":
            continue
        key = tuple(t.values[i] for i in pk)
        cur = best.get(key)
        if cur is None or (t.xmin, t.block, t.lp) > (cur.xmin, cur.block, cur.lp):
            best[key] = t
    rows = [best[k].values for k in sorted(best)]
    C.write_csv(args.out, attrs, rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
