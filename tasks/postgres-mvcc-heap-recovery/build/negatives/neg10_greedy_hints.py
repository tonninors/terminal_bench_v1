#!/usr/bin/env python3
"""Negative 10 - greedy one-hop propagation from the direct hints.

Applies the hints, then a single round of the obvious foreign-key rule
(a hint-committed child forces its only provider committed), and defaults
everything still open to aborted.  The deeper links - abort forced by a
provider three hops away, uniqueness interacting with a delete - never
fire."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as C            # noqa: E402

G = C.G


def main() -> int:
    args = C.standard_args(__doc__).parse_args()
    js, relations, log, snap, ev = C.load(args)
    asg = dict(ev.hints)

    # one greedy FK hop: for each hint-committed child row, if exactly one
    # unresolved transaction inserted a matching parent key, mark it committed
    by_rel = {r.name: r for r in relations}
    for rel in relations:
        for fk in rel.fks:
            idx = [rel.colnames.index(c) for c in fk["columns"]]
            parent = by_rel[fk["references"]]
            for t in rel.tuples:
                if asg.get(t.xmin) != "committed":
                    continue
                ref = tuple(t.values[i] for i in idx)
                providers = {p.xmin for p in parent.tuples
                             if parent.key_of(p)[:len(ref)] == ref
                             or tuple(p.values[parent.colnames.index(c)]
                                      for c in fk["referenced_columns"]) == ref}
                unresolved = [x for x in providers if x in ev.unknown]
                if len(unresolved) == 1:
                    asg.setdefault(unresolved[0], "committed")
    vis = C.visible_under(relations, snap, log, ev, asg, default="aborted")
    C.emit(args, relations[0], vis)
    return 0


if __name__ == "__main__":
    sys.exit(main())
