#!/usr/bin/env python3
"""Negative G - an otherwise correct CSV with one primary key damaged.

Takes a correct answer and either drops one row or repeats one row, leaving
every other value untouched.  This is the near-miss case: the MVCC reasoning is
right but the result set is off by exactly one key.

    negative_g_key_damage.py --src correct.csv --out damaged.csv --mode drop
    negative_g_key_damage.py --src correct.csv --out damaged.csv --mode duplicate
"""
from __future__ import annotations

import argparse
import csv
import sys


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--mode", choices=["drop", "duplicate"], default="drop")
    ap.add_argument("--index", type=int, default=7, help="which data row to touch")
    args = ap.parse_args()

    with open(args.src, encoding="utf-8", newline="") as fh:
        rows = [r for r in csv.reader(fh) if r]
    header, data = rows[0], rows[1:]
    if not data:
        raise SystemExit("source CSV has no data rows")
    i = args.index % len(data)
    if args.mode == "drop":
        victim = data.pop(i)
        print("dropped primary key %s" % victim[0])
    else:
        data.insert(i, list(data[i]))
        print("duplicated primary key %s" % data[i][0])

    with open(args.out, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(header)
        w.writerows(data)
    print("wrote %d row(s) to %s" % (len(data), args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
