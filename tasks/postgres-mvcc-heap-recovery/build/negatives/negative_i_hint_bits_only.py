#!/usr/bin/env python3
"""Negative I - trust the tuple hint bits instead of the commit log.

HEAP_XMIN_COMMITTED / HEAP_XMAX_INVALID are set lazily, so a large share of
the tuples on these pages carry no hint at all.  Reading the hints as the
authoritative commit state, and skipping the snapshot, is a common shortcut."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as C            # noqa: E402


HEAP_XMIN_COMMITTED = 0x0100
HEAP_XMAX_INVALID = 0x0800


def main() -> int:
    args = C.standard_args(__doc__).parse_args()
    js, attrs, snap, status, tuples = C.load(args)
    rows = []
    for t in tuples:
        if not (t.infomask & HEAP_XMIN_COMMITTED):
            continue
        if not (t.infomask & HEAP_XMAX_INVALID) and t.xmax:
            continue
        rows.append(t.values)
    pk = C.pk_positions(js, attrs)
    rows.sort(key=lambda v: tuple(str(v[i]) for i in pk))
    C.write_csv(args.out, attrs, rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
