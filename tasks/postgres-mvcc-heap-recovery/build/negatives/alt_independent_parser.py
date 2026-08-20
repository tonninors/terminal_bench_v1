#!/usr/bin/env python3
"""Alternative correct solution - a second, independent page-level recovery.

Written from scratch against the PostgreSQL 16 on-disk format: it shares no code
with solution/golden_recover.py, lays the parser out differently (memoryview
slices and an explicit attribute cursor rather than per-tuple objects), decodes
the commit log and the subtransaction map with its own bit arithmetic and its own
recursive topmost-parent walk, states the visibility rules as an explicit
decision table, and deliberately writes the output in a *different but permitted*
shape - CRLF line endings, booleans spelled true/false, rows in descending
primary key order.

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

# SLRU geometry, spelled out independently of the oracle
PAGES_PER_SEG = 32
CLOG_PER_PAGE = BLOCK * 4            # four transactions per byte
SUB_PER_PAGE = BLOCK // 4            # one 4-byte parent xid per entry

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


class Slru:
    """Segment files keyed by segment number, addressed by entries-per-page."""

    def __init__(self, directory, per_page):
        self.per_page = per_page
        self.segs = {}
        for f in sorted(Path(directory).iterdir()):
            if f.is_file() and len(f.name) == 4:
                self.segs[int(f.name, 16)] = f.read_bytes()
        if not self.segs:
            raise SystemExit("no SLRU segments in %s" % directory)

    def offset(self, xid):
        page, within = divmod(xid, self.per_page)
        seg, page_in_seg = divmod(page, PAGES_PER_SEG)
        return self.segs.get(seg), page_in_seg * BLOCK, within


def commit_bits(clog, xid):
    blob, base, within = clog.offset(xid)
    if blob is None:
        raise SystemExit("pg_xact segment missing for xid %d" % xid)
    return (blob[base + within // 4] >> ((within % 4) * 2)) & 3


def parent_of(subtrans, xid):
    blob, base, within = subtrans.offset(xid)
    if blob is None:
        return 0
    return struct.unpack_from("<I", blob, base + within * 4)[0]


def main() -> int:
    ap = argparse.ArgumentParser()
    here = Path(__file__).resolve().parent.parent.parent / "artifacts"
    ap.add_argument("--heap", default=str(here / "heap_pages.bin"))
    ap.add_argument("--pg-xact", dest="pg_xact", default=str(here / "pg_xact"))
    ap.add_argument("--pg-subtrans", dest="pg_subtrans",
                    default=str(here / "pg_subtrans"))
    ap.add_argument("--pg-multixact", dest="pg_multixact",
                    default=str(here / "pg_multixact"))
    ap.add_argument("--schema", default=str(here / "table_schema.json"))
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    meta = json.loads(Path(a.schema).read_text(encoding="utf-8"))
    cols = [(c["name"], c["type"]) for c in meta["columns"]]
    names = [c[0] for c in cols]
    pk = [names.index(c) for c in meta["primary_key"]]
    s_xmin, s_xmax = meta["snapshot_xmin"], meta["snapshot_xmax"]
    xip = frozenset(meta["snapshot_xip"])

    clog = Slru(a.pg_xact, CLOG_PER_PAGE)
    subtrans = Slru(a.pg_subtrans, SUB_PER_PAGE)
    mx_root = Path(a.pg_multixact)
    mx_offsets = Slru(mx_root / "offsets", BLOCK // 4)          # 2048 per page
    mx_members_raw = Slru(mx_root / "members", (BLOCK // 20) * 4)  # 1636 per page

    def mx_members(mxid):
        """[(xid, status)]: groups of 4 flag bytes then 4 xids, 20 bytes each.
        Member slot 0 is reserved; offsets[mxid+1] bounds the list."""
        lo, lo_base, lo_i = mx_offsets.offset(mxid)
        hi, hi_base, hi_i = mx_offsets.offset(mxid + 1)
        start = struct.unpack_from("<I", lo, lo_base + lo_i * 4)[0]
        end = struct.unpack_from("<I", hi, hi_base + hi_i * 4)[0]
        if start == 0 or end < start:
            raise SystemExit("member range of multi %d is unbounded" % mxid)
        out = []
        for i in range(start, end):
            blob, base, within = mx_members_raw.offset(i)
            group, idx = divmod(within, 4)
            gb = base + group * 20
            out.append((struct.unpack_from("<I", blob, gb + 4 + idx * 4)[0],
                        blob[gb + idx]))
        return out

    def mx_update_member(mxid):
        ups = [x for x, f in mx_members(mxid) if f in (4, 5)]
        return ups[0] if ups else None

    def top_of(x):
        """Climb pg_subtrans to the enclosing top-level transaction."""
        guard = 0
        while True:
            up = parent_of(subtrans, x)
            if up == 0 or up >= x or guard > 64:
                return x
            x = up
            guard += 1

    def finished_state(x):
        """committed / aborted / running, per xid.  A SUB_COMMITTED entry means
        the child is done but the parent has the final say."""
        guard = 0
        while True:
            bits = commit_bits(clog, x)
            if bits == 1:
                return "committed"
            if bits == 2:
                return "aborted"
            if bits == 0:
                return "running"
            up = parent_of(subtrans, x)          # 3 == SUB_COMMITTED
            if up == 0 or up >= x or guard > 64:
                return "running"
            x = up
            guard += 1

    def unfinished(x):
        """True when x had not completed as of the snapshot.

        The xip list carries top-level xids only, so a subtransaction has to be
        resolved through pg_subtrans before it can be looked up."""
        if x >= s_xmax:
            return True
        if x < s_xmin:
            return False
        if x in xip:
            return True
        top = top_of(x)
        if top == x:
            return False
        if top >= s_xmax:
            return True
        if top < s_xmin:
            return False
        return top in xip

    def committed_by_snapshot(x):
        return finished_state(x) == "committed" and not unfinished(x)

    visible = {}
    for xmin, xmax, mask, values in live_tuples(Path(a.heap).read_bytes(), cols):
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
        elif mask & 0x1000:
            # a MultiXactId: only the (at most one) update member can delete
            u = mx_update_member(xmax)
            deleted = u is not None and committed_by_snapshot(u)
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
