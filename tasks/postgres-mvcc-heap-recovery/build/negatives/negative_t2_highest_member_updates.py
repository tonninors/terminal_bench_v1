#!/usr/bin/env python3
"""Negative T2 - assume the highest-xid member is the updater.

Decodes the member list but replaces the status flags with a plausible
heuristic: the newest member must be the one that changed the row.  On
locker-only multis that promotes a committed locker to updater and deletes a
live row; on updater multis whose locker began after the updater it picks the
wrong member."""
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
        members = log.multi.members(t.xmax)
        return max(x for x, _flag in members)        # flags ignored
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
