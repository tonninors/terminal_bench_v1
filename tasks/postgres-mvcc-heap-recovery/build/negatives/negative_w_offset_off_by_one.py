#!/usr/bin/env python3
"""Negative W - mishandle the reserved member offset 0.

pg_multixact/members index 0 is reserved so a zero offsets entry can mean
'never written'; the first real member lives at index 1.  This solver assumes
the array is plainly 0-based and subtracts one from every offsets entry, so
each multi's member window shifts and xids are read against the wrong flag
bytes."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as C            # noqa: E402

G = C.G


def resolve(t, snap, log):
    if t.infomask & 0x0800 or not t.xmax:
        return None
    if t.infomask & 0x1000:
        m = t.xmax
        start, end = log.multi._offset(m) - 1, log.multi._offset(m + 1) - 1
        ups = []
        for i in range(max(start, 0), max(end, 0)):
            import struct as _s
            pageno, within = divmod(i, G.MULTIXACT_MEMBERS_PER_PAGE)
            segno, page = divmod(pageno, G.SLRU_PAGES_PER_SEGMENT)
            blob = log.multi.member_segs[segno]
            group, idx = divmod(within, G.MULTIXACT_MEMBERS_PER_GROUP)
            base = page * G.BLCKSZ + group * G.MULTIXACT_MEMBERGROUP_SIZE
            flag = blob[base + idx]
            xid = _s.unpack_from("<I", blob, base + 4 + idx * 4)[0]
            if flag in G.MXS_IS_UPDATE and xid:
                ups.append(xid)
        return ups[0] if ups else None
    if G.xmax_is_locked_only(t.infomask):
        return None
    return t.xmax


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
