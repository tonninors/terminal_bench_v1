#!/usr/bin/env python3
"""Negative 12 - the V3/V4 pipeline verbatim: parse, look up, apply rules.

A faithful port of the solver that beat the earlier fixtures: heap parsing,
clog lookup (zero bits = in progress), subtransaction resolution, snapshot
rules.  It has no concept of missing evidence, so every transaction whose
outcome never reached the on-disk clog is treated as still running and its
durable work vanishes."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as C            # noqa: E402

G = C.G


def main() -> int:
    args = C.standard_args(__doc__).parse_args()
    js, relations, log, snap, ev = C.load(args)
    out_rel = relations[0]

    def outcome(xid):
        st = log.recorded_state(xid)
        return st                       # None = in progress, as V3/V4 read it

    vis = [t for t in out_rel.tuples
           if G.tuple_visible(t, snap, log, outcome)]
    C.emit(args, out_rel, vis)
    return 0


if __name__ == "__main__":
    sys.exit(main())
