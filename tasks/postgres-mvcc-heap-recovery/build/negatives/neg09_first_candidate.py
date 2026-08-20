#!/usr/bin/env python3
"""Negative 9 - accept the first locally plausible candidate.

Enumerates candidate outcome assignments in the "conservative" order a crash
recovery mindset suggests (crashed transactions abort, so try aborted first)
and stops at the first assignment that satisfies the hint bits and key
uniqueness - without foreign keys, and without proving that no other
assignment survives.  Locally everything looks fine; globally it is the wrong
member of the surviving family, and two large committed batches are treated
as rolled back.
"""
from __future__ import annotations

import itertools
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as C            # noqa: E402

G = C.G


def main() -> int:
    args = C.standard_args(__doc__).parse_args()
    js, relations, log, snap, ev = C.load(args)
    engine = G.Engine(relations, log, snap, ev)

    ks = engine.unknowns
    for bits in itertools.product(("aborted", "committed"), repeat=len(ks)):
        asg = dict(zip(ks, bits))
        if not engine.hint_ok(asg):
            continue
        state = engine.visible_state(asg)
        ok = True
        for rel in relations:
            seen = set()
            for t in state[rel.name]:
                k = rel.key_of(t)
                if k in seen:
                    ok = False
                    break
                seen.add(k)
            if not ok:
                break
        if ok:
            vis = C.visible_under(relations, snap, log, ev, asg)
            C.emit(args, relations[0], vis)
            return 0
    raise SystemExit("no candidate found")


if __name__ == "__main__":
    sys.exit(main())
