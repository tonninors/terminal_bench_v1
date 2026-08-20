#!/usr/bin/env python3
"""Negative 7 - enumerate with hints and key uniqueness, but no foreign keys.

Foreign-key consistency is what forces the committed side of every indirect
chain (a hinted child row proves its only possible provider committed).
Without it the enumeration keeps several assignments alive, and the
tie-break picks the wrong survivor."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as C            # noqa: E402

G = C.G


def main() -> int:
    args = C.standard_args(__doc__).parse_args()
    js, relations, log, snap, ev = C.load(args)
    engine = G.Engine(relations, log, snap, ev)

    survivors = []
    for asg in engine.candidates():
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
            survivors.append(asg)
    asg = min(survivors, key=lambda a: sum(v == "committed"
                                           for v in a.values()))
    vis = C.visible_under(relations, snap, log, ev, asg)
    C.emit(args, relations[0], vis)
    return 0


if __name__ == "__main__":
    sys.exit(main())
