#!/usr/bin/env python3
"""Negative 6 - recover each relation independently.

Runs a real enumeration engine, but against ONE relation at a time: hint bits
stamped on the other two relations never become facts, cross-relation
atomicity is never enforced, and foreign keys cannot participate.  Over the
output relation alone almost nothing is forced - both outcomes of nearly
every unresolved transaction leave a locally consistent table (uniqueness
only forbids the both-committed case; both-aborted merely loses the key).
Where the relation's own evidence is ambiguous the solver falls back to the
documented zero-fill default, aborted, and the recovery collapses to the
zero-fill answer.
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
    out_rel = relations[0]

    # evidence and engine built from the output relation ALONE
    solo = G.Evidence([out_rel], log, snap)
    engine = G.Engine([out_rel], log, snap, solo)

    values_seen = {x: set() for x in engine.unknowns}
    survivors = 0
    for asg in engine.candidates():
        if not engine.hint_ok(asg):
            continue
        state = engine.visible_state(asg)
        if not engine.constraints_ok(state):
            continue
        survivors += 1
        for x, v in asg.items():
            values_seen[x].add(v)
    if not survivors:
        raise SystemExit("no locally consistent candidate")

    # keep only what the relation itself forces; everything ambiguous gets
    # the zero-fill default
    asg = {x: vs.pop() if len(vs) == 1 else "aborted"
           for x, vs in ((x, set(vs)) for x, vs in values_seen.items())}
    print("locally consistent candidates: %d; locally forced xids: %d"
          % (survivors, sum(1 for x in values_seen
                            if len(values_seen[x]) == 1)))
    vis = C.visible_under(relations, snap, log, ev, asg, default="aborted")
    C.emit(args, out_rel, vis)
    return 0


if __name__ == "__main__":
    sys.exit(main())
