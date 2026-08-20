#!/usr/bin/env python3
"""Negative H - the right rows, written against the wrong output contract.

Three variants, each violating one explicit requirement of the prompt:

    --mode nulls    SQL NULL written as an empty field instead of \\N
    --mode order    schema columns emitted in a different order
    --mode noheader the header line omitted

Everything else - the set of visible primary keys and every value - is correct,
so these isolate the output contract from the MVCC reasoning.
"""
from __future__ import annotations

import argparse
import csv
import sys


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--mode", choices=["nulls", "order", "noheader"],
                    default="nulls")
    args = ap.parse_args()

    with open(args.src, encoding="utf-8", newline="") as fh:
        rows = [r for r in csv.reader(fh) if r]
    header, data = rows[0], rows[1:]

    if args.mode == "nulls":
        data = [["" if v == "\\N" else v for v in row] for row in data]
    elif args.mode == "order":
        perm = list(range(len(header)))
        perm[0], perm[-1] = perm[-1], perm[0]
        header = [header[i] for i in perm]
        data = [[row[i] for i in perm] for row in data]
    elif args.mode == "noheader":
        header = None

    with open(args.out, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        if header is not None:
            w.writerow(header)
        w.writerows(data)
    print("wrote %d row(s) to %s in mode %s" % (len(data), args.out, args.mode))
    return 0


if __name__ == "__main__":
    sys.exit(main())
