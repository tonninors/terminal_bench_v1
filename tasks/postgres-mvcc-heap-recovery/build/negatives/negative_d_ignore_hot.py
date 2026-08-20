#!/usr/bin/env python3
"""Negative D - ignore what HOT does to a page's line pointers.

Applies the visibility rules exactly as the oracle does, but walks the line
pointer array naively: a HOT redirect is followed and its target emitted as if
the redirect slot were itself a row version, and pruned (LP_DEAD) slots whose
bytes are still readable are decoded too.

`lp_off` in an LP_REDIRECT item is an OffsetNumber, not a byte offset, and the
slot holds no tuple of its own - so this both double-counts live versions and
resurrects storage that HOT pruning already retired.
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as C            # noqa: E402

G = C.G
PAGE_HEADER_SIZE = 24
ITEM_ID_SIZE = 4


def line_pointers(page):
    pd_lower = struct.unpack_from("<H", page, 12)[0]
    n = (pd_lower - PAGE_HEADER_SIZE) // ITEM_ID_SIZE
    out = []
    for i in range(n):
        raw = struct.unpack_from("<I", page, PAGE_HEADER_SIZE + i * ITEM_ID_SIZE)[0]
        out.append((i + 1, raw & 0x7FFF, (raw >> 15) & 0x3, (raw >> 17) & 0x7FFF))
    return out


def main() -> int:
    args = C.standard_args(__doc__).parse_args()
    js, attrs, snap, status, _ = C.load(args)
    heap = Path(args.heap).read_bytes()
    notes = {"frozen": 0, "hint_conflicts": 0, "lock_only": 0}

    rows = []
    for blk in range(len(heap) // 8192):
        page = heap[blk * 8192:(blk + 1) * 8192]
        lps = line_pointers(page)
        by_offnum = {lp: (off, flags, ln) for lp, off, flags, ln in lps}
        for lp, off, flags, ln in lps:
            if flags == 0:                      # LP_UNUSED
                continue
            if flags == 2:                      # LP_REDIRECT: chase it and emit
                target = by_offnum.get(off)
                if not target or target[1] != 1:
                    continue
                off, ln = target[0], target[2]
            if ln < 23 or off + ln > len(page):
                continue
            try:
                t = G.parse_tuple(page, blk, lp, off, ln, attrs)
            except SystemExit:
                continue
            if G.tuple_visible(t, snap, status, notes):
                rows.append(t.values)

    pk = C.pk_positions(js, attrs)
    rows.sort(key=lambda v: tuple(str(v[i]) for i in pk))
    C.write_csv(args.out, attrs, rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
