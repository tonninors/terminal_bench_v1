#!/usr/bin/env python3
"""Negative D2 - treat heap-only tuples as internal HOT bookkeeping.

Applies the visibility rules correctly but skips every tuple carrying
HEAP_ONLY_TUPLE, on the theory that a heap-only version is an implementation
detail of a HOT chain and the real row lives at the chain root.  A heap-only
tuple is a full row version with no index entry, and on these pages it is
frequently the version the snapshot can actually see."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as C            # noqa: E402


HEAP_ONLY_TUPLE = 0x8000


def main() -> int:
    args = C.standard_args(__doc__).parse_args()
    js, attrs, snap, status, tuples = C.load(args)
    notes = {"frozen": 0, "hint_conflicts": 0, "lock_only": 0}
    rows = []
    for t in tuples:
        if t.infomask2 & HEAP_ONLY_TUPLE:
            continue
        if C.G.tuple_visible(t, snap, status, notes):
            rows.append(t.values)
    pk = C.pk_positions(js, attrs)
    rows.sort(key=lambda v: tuple(str(v[i]) for i in pk))
    C.write_csv(args.out, attrs, rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
