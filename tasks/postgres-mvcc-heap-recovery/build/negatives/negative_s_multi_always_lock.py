#!/usr/bin/env python3
"""Negative S - treat every MultiXact xmax as a pure row lock.

Notices HEAP_XMAX_IS_MULTI but never opens pg_multixact: multis do often mean
locks, so the guess keeps every multi-stamped tuple alive.  It is wrong for
every multi whose member list contains an update member that committed before
the snapshot - those old versions are dead, and this solver duplicates their
keys."""
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
        return None                  # "a multi is just locks"
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
