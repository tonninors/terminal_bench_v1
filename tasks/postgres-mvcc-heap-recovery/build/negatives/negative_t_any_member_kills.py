#!/usr/bin/env python3
"""Negative T - any committed MultiXact member kills the tuple.

Opens pg_multixact and decodes the members correctly, but ignores the
per-member status flags that separate lockers from the at-most-one updater.
A committed locker is enough to delete the row under this rule, so every
locker-only multi with a committed member loses its tuple."""
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
        for x, _flag in log.multi.members(t.xmax):   # flags never consulted
            if log(x) == "committed" and not snap.in_progress(x, log):
                return x
        return None
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
