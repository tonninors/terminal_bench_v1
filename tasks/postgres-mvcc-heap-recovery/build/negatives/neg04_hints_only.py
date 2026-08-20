#!/usr/bin/env python3
"""Negative 4 - hint bits are the only evidence; unhinted unknowns abort.

Resolves every unresolved transaction that carries a stamped hint bit
anywhere, and defaults the rest to aborted.  The eight indirect
transactions - recoverable only through constraint reconciliation - all
land on the default, and half of them are wrong."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as C            # noqa: E402

G = C.G


DEFAULT = 'aborted'
ASSIGN = lambda ev: dict(ev.hints)


def main() -> int:
    args = C.standard_args(__doc__).parse_args()
    js, relations, log, snap, ev = C.load(args)
    vis = C.visible_under(relations, snap, log, ev, ASSIGN(ev), default=DEFAULT)
    C.emit(args, relations[0], vis)
    return 0


if __name__ == "__main__":
    sys.exit(main())
