#!/usr/bin/env python3
"""Negative Z - a frozen tuple is visible, full stop.

Freezing only settles the INSERTING side: a frozen xmin predates every
snapshot.  The xmax still governs deletion, and this fixture's frozen base
rows were updated, deleted and locked afterwards.  Short-circuiting on the
frozen mask resurrects every one of them next to its successor."""
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
        if (t.infomask & 0x0300) == 0x0300:
            rows.append(t.values)                    # xmax never examined
            continue
        if t.infomask & 0x0200:
            continue
        if log(t.xmin) != "committed" or snap.in_progress(t.xmin, log):
            continue
        u = C.deleting_xid(t, log)
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
