#!/usr/bin/env python3
"""Negative K - the output is not a CSV table at all.

Stands in for a solver that produced something unparsable at the expected path.

    --mode json      a JSON document
    --mode binary    raw bytes, including NULs
    --mode empty     a zero-length file
"""
from __future__ import annotations

import argparse
import sys

JSON_BODY = '{"rows": [{"account_id": 1, "balance_cents": 250001}]}\n'


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--mode", choices=["json", "binary", "empty"], default="json")
    a = ap.parse_args()
    if a.mode == "json":
        with open(a.out, "w", encoding="utf-8") as fh:
            fh.write(JSON_BODY)
    elif a.mode == "binary":
        with open(a.out, "wb") as fh:
            fh.write(b"\x00\x01\x02PGHEAP\x00\xff" * 32)
    else:
        open(a.out, "wb").close()
    print("wrote a %s file to %s" % (a.mode, a.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
