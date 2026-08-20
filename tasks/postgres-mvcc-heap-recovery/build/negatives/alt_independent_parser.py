#!/usr/bin/env python3
"""Alternative correct solution - a second, independent page-level recovery.

Written from scratch against the PostgreSQL 16 on-disk format: it shares no code
with solution/golden_recover.py, lays the parser out differently (memoryview
slices and an explicit attribute cursor rather than per-tuple objects), states
the visibility rules as an explicit decision table, and deliberately writes the
output in a *different but permitted* shape - CRLF line endings, booleans
spelled true/false, rows in descending primary key order.

Its purpose is to prove the verifier grades the outcome, not the method.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import struct
import sys
from pathlib import Path

BLOCK = 8192
PAGE_HDR = 24
EPOCH = dt.date(2000, 1, 1)

FIXED = {
    "smallint": (2, 2), "integer": (4, 4), "bigint": (8, 8),
    "boolean": (1, 1), "date": (4, 4),
}
VARLEN = {"text", "character varying", "varchar", "character", "char", "bpchar"}


def basetype(t):
    return t.split("(")[0].strip().lower()


def align_up(v, a):
    return (v + a - 1) & ~(a - 1)


def varlena(mv, i):
    """(payload_bytes, consumed) for the varlena at index i."""
    first = mv[i]
    if first & 0x01:
        if first == 0x01:
            raise SystemExit("out-of-line TOAST pointer encountered")
        size = first >> 1
        return bytes(mv[i + 1:i + size]), size
    word = struct.unpack_from("<I", mv, i)[0]
    if word & 0x03 == 0x02:
        raise SystemExit("compressed datum encountered")
    size = word >> 2
    return bytes(mv[i + 4:i + size]), size


def decode_tuple(mv, cols):
    """(xmin, xmax, infomask, values) for one heap tuple."""
    xmin, xmax = struct.unpack_from("<II", mv, 0)
    infomask2, infomask, hoff = struct.unpack_from("<HHB", mv, 18)
    natts = infomask2 & 0x07FF

    bits = None
    if infomask & 0x0001:
        bits = mv[23:23 + (natts + 7) // 8]

    out, cur = [], hoff
    for idx, (name, typ) in enumerate(cols):
        if idx >= natts:
            out.append(None)
            continue
        if bits is not None and not (bits[idx // 8] >> (idx % 8)) & 1:
            out.append(None)
            continue
        base = basetype(typ)
        if base in VARLEN:
            # a varlena whose first byte is non-zero is never padded
            if mv[cur] == 0:
                cur = align_up(cur, 4)
            payload, used = varlena(mv, cur)
            out.append(payload.decode("utf-8"))
            cur += used
        else:
            size, al = FIXED[base]
            cur = align_up(cur, al)
            chunk = bytes(mv[cur:cur + size])
            if base == "boolean":
                out.append(chunk[0] != 0)
            elif base == "date":
                out.append(EPOCH + dt.timedelta(
                    days=int.from_bytes(chunk, "little", signed=True)))
            else:
                out.append(int.from_bytes(chunk, "little", signed=True))
            cur += size
    return xmin, xmax, infomask, out


def live_tuples(heap, cols):
    """Every LP_NORMAL tuple; redirect, dead and unused slots carry no row."""
    for blk in range(len(heap) // BLOCK):
        page = memoryview(heap)[blk * BLOCK:(blk + 1) * BLOCK]
        lower, upper = struct.unpack_from("<HH", page, 12)
        if lower == 0:
            continue
        for i in range((lower - PAGE_HDR) // 4):
            item = struct.unpack_from("<I", page, PAGE_HDR + i * 4)[0]
            flags = (item >> 15) & 0x3
            if flags != 1:
                continue
            off, ln = item & 0x7FFF, (item >> 17) & 0x7FFF
            yield decode_tuple(page[off:off + ln], cols)


def main() -> int:
    ap = argparse.ArgumentParser()
    here = Path(__file__).resolve().parent.parent.parent / "artifacts"
    ap.add_argument("--heap", default=str(here / "heap_pages.bin"))
    ap.add_argument("--tx", default=str(here / "tx_status.csv"))
    ap.add_argument("--schema", default=str(here / "table_schema.json"))
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    meta = json.loads(Path(a.schema).read_text(encoding="utf-8"))
    cols = [(c["name"], c["type"]) for c in meta["columns"]]
    names = [c[0] for c in cols]
    pk = [names.index(c) for c in meta["primary_key"]]
    s_xmin, s_xmax = meta["snapshot_xmin"], meta["snapshot_xmax"]
    xip = frozenset(meta["snapshot_xip"])

    clog = {}
    with open(a.tx, encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            clog[int(r["xid"])] = r["status"].strip().lower()

    def unfinished(x):
        """True when x had not committed as of the snapshot."""
        if x >= s_xmax:
            return True
        if x < s_xmin:
            return False
        return x in xip

    def committed_by_snapshot(x):
        return clog[x] == "committed" and not unfinished(x)

    visible = {}
    for xmin, xmax, mask, values in live_tuples(Path(a.heap).read_bytes(), cols):
        if mask & 0x1000:
            raise SystemExit("MultiXact xmax is out of scope for this fixture")
        if (mask & 0x0300) == 0x0300:
            inserted_ok = True                      # frozen
        elif mask & 0x0200:
            inserted_ok = False                     # hint: inserter aborted
        else:
            inserted_ok = committed_by_snapshot(xmin)
        if not inserted_ok:
            continue

        if mask & 0x0800 or xmax == 0 or mask & 0x0080:
            deleted = False                         # invalid / absent / lock-only
        else:
            deleted = committed_by_snapshot(xmax)
        if deleted:
            continue

        key = tuple(values[i] for i in pk)
        if key in visible:
            raise SystemExit("two visible versions of primary key %r" % (key,))
        visible[key] = values

    def cell(v):
        if v is None:
            return "\\N"
        if isinstance(v, bool):
            return "true" if v else "false"       # permitted alternative spelling
        if isinstance(v, dt.date):
            return v.isoformat()
        return str(v)

    with open(a.out, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, lineterminator="\r\n")   # CRLF, also permitted
        w.writerow(names)
        for key in sorted(visible, reverse=True):   # descending, order is free
            w.writerow([cell(v) for v in visible[key]])
    print("independent parser wrote %d visible row(s) to %s"
          % (len(visible), a.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
