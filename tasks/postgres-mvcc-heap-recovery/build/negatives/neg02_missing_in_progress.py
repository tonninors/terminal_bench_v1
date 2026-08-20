#!/usr/bin/env python3
"""Negative 2 - take the zero clog bits at face value: still in progress.

Zero bits nominally mean IN_PROGRESS, and an in-progress transaction is
invisible to every snapshot.  But the supplied snapshot itself refutes that
reading for every xid below snapshot_xmin: those transactions had finished.
Face value loses the same rows the zero-fill remedy loses, for a subtler
reason."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as C            # noqa: E402

G = C.G


DEFAULT = None
ASSIGN = lambda ev: {}


def main() -> int:
    args = C.standard_args(__doc__).parse_args()
    js, relations, log, snap, ev = C.load(args)
    vis = C.visible_under(relations, snap, log, ev, ASSIGN(ev), default=DEFAULT)
    C.emit(args, relations[0], vis)
    return 0


if __name__ == "__main__":
    sys.exit(main())
