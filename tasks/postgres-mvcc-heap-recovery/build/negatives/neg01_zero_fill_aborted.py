#!/usr/bin/env python3
"""Negative 1 - the famous zero-fill remedy: missing clog means aborted.

The widely documented rescue for damaged pg_xact is to pad the file with
zero bytes; zero status bits read as 'in progress', which recovery then
treats as crashed, i.e. aborted.  Applied here it silently discards every
transaction whose commit never reached the on-disk clog - a large slice of
the durable recent history."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as C            # noqa: E402

G = C.G


DEFAULT = 'aborted'
ASSIGN = lambda ev: {}


def main() -> int:
    args = C.standard_args(__doc__).parse_args()
    js, relations, log, snap, ev = C.load(args)
    vis = C.visible_under(relations, snap, log, ev, ASSIGN(ev), default=DEFAULT)
    C.emit(args, relations[0], vis)
    return 0


if __name__ == "__main__":
    sys.exit(main())
