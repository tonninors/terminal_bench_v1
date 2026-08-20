#!/usr/bin/env python3
"""Negative Q - give a subtransaction its parent's commit status.

Resolves each subxid to its topmost parent through pg_subtrans - and then uses
that parent's commit status as the subtransaction's own.

That is the wrong half of the rule.  pg_subtrans decides which transaction the
snapshot should be tested against; pg_xact still decides, per xid, whether the
work survived.  A savepoint rolled back inside a transaction that went on to
commit is ABORTED in its own right, so inheriting the parent's COMMITTED status
resurrects discarded updates and re-applies deletions that were undone."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as C            # noqa: E402


def main() -> int:
    args = C.standard_args(__doc__).parse_args()
    js, attrs, snap, log, tuples = C.load(args)

    def state(xid):
        return log(log.topmost(xid))            # parent's verdict, not the child's

    notes = {"frozen": 0, "hint_conflicts": 0, "lock_only": 0}
    rows = []
    for t in tuples:
        if state(t.xmin) != "committed":
            continue
        if snap.in_progress(t.xmin, log):
            continue
        if not (t.infomask & 0x0800) and t.xmax \
                and not C.G.xmax_is_locked_only(t.infomask):
            if state(t.xmax) == "committed" and not snap.in_progress(t.xmax, log):
                continue
        rows.append(t.values)
    pk = C.pk_positions(js, attrs)
    rows.sort(key=lambda v: tuple(str(v[i]) for i in pk))
    C.write_csv(args.out, attrs, rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
