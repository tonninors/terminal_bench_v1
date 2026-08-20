#!/usr/bin/env python3
"""Negative 8 - enumerate with hints and foreign keys, but no PK/UNIQUE.

Key uniqueness is what forces the aborted side of the chains (two committed
versions of one key are impossible).  Without it several assignments
survive, and the tie-break resurrects rolled-back work - duplicate keys
included."""
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

    keysets_needed = {r.name: r for r in relations}
    survivors = []
    for asg in engine.candidates():
        if not engine.hint_ok(asg):
            continue
        state = engine.visible_state(asg)
        ok = True
        keys = {}
        for rel in relations:
            ks = {}
            for t in state[rel.name]:
                ks[rel.key_of(t)] = t     # duplicates silently collapse
            keys[rel.name] = ks
        for rel in relations:
            for fk in rel.fks:
                idx = [rel.colnames.index(c) for c in fk["columns"]]
                parent = keys[fk["references"]]
                for t in state[rel.name]:
                    ref = tuple(t.values[i] for i in idx)
                    if any(v is None for v in ref):
                        continue
                    if ref not in parent:
                        ok = False
                        break
                if not ok:
                    break
            if not ok:
                break
        if ok:
            survivors.append(asg)
    asg = max(survivors, key=lambda a: sum(v == "committed"
                                           for v in a.values()))
    vis = C.visible_under(relations, snap, log, ev, asg)
    C.emit(args, relations[0], vis)
    return 0


if __name__ == "__main__":
    sys.exit(main())
