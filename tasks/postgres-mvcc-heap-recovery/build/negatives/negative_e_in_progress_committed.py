#!/usr/bin/env python3
"""Negative E - treat still-running transactions as committed.

Uncommitted inserts appear, uncommitted deletes remove live rows, and the
uncommitted side of an in-flight UPDATE replaces the version the snapshot
should still see."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as C            # noqa: E402


def main() -> int:
    args = C.standard_args(__doc__).parse_args()
    js, attrs, snap, status, tuples = C.load(args)
    rows = []
    for t in tuples:
        if status(t.xmin) == "aborted":
            continue
        if snap.in_progress(t.xmin, status) and status(t.xmin) == "committed":
            continue
        u = C.deleting_xid(t, status)
        if u is not None and status(u) != "aborted":
            if not (snap.in_progress(u, status) and status(u) == "committed"):
                continue
        rows.append(t.values)
    pk = C.pk_positions(js, attrs)
    rows.sort(key=lambda v: tuple(str(v[i]) for i in pk))
    C.write_csv(args.out, attrs, rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
