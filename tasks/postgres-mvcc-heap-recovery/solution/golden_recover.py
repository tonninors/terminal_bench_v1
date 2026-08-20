#!/usr/bin/env python3
"""Reference recovery of the MVCC-visible rows of a PostgreSQL 16 heap.

Reads only the three files the solver is given - the raw relation blocks, the
transaction states and the schema/snapshot description - and reconstructs the
logical table as PostgreSQL would have seen it under the supplied snapshot.

    golden_recover.py [--heap /app/heap_pages.bin] [--tx /app/tx_status.csv]
                      [--schema /app/table_schema.json] [--out /app/recovered.csv]
                      [--report]

No hidden expected answer is consulted and no row is hard-coded anywhere in this
file.  Structure references: src/include/storage/bufpage.h (PageHeaderData,
ItemIdData), src/include/access/htup_details.h (HeapTupleHeaderData, t_infomask
bits, att_align/att_addlength) and src/backend/utils/time/snapmgr.c plus
heapam_visibility.c (HeapTupleSatisfiesMVCC, XidInMVCCSnapshot).
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import struct
import sys
from pathlib import Path

# ---------------------------------------------------------------- page layout
PAGE_HEADER_SIZE = 24          # sizeof(PageHeaderData) up to pd_linp[]
ITEM_ID_SIZE = 4

LP_UNUSED, LP_NORMAL, LP_REDIRECT, LP_DEAD = 0, 1, 2, 3

# ---------------------------------------------------------------- tuple header
HEAP_HASNULL = 0x0001
HEAP_HASVARWIDTH = 0x0002
HEAP_HASEXTERNAL = 0x0004
HEAP_XMAX_KEYSHR_LOCK = 0x0010
HEAP_COMBOCID = 0x0020
HEAP_XMAX_EXCL_LOCK = 0x0040
HEAP_XMAX_LOCK_ONLY = 0x0080
# HEAP_LOCK_MASK = HEAP_XMAX_SHR_LOCK | HEAP_XMAX_EXCL_LOCK | HEAP_XMAX_KEYSHR_LOCK
HEAP_LOCK_MASK = HEAP_XMAX_KEYSHR_LOCK | HEAP_XMAX_EXCL_LOCK
HEAP_XMIN_COMMITTED = 0x0100
HEAP_XMIN_INVALID = 0x0200
HEAP_XMIN_FROZEN = HEAP_XMIN_COMMITTED | HEAP_XMIN_INVALID
HEAP_XMAX_COMMITTED = 0x0400
HEAP_XMAX_INVALID = 0x0800
HEAP_XMAX_IS_MULTI = 0x1000

HEAP_NATTS_MASK = 0x07FF
HEAP_HOT_UPDATED = 0x4000
HEAP_ONLY_TUPLE = 0x8000

INVALID_XID = 0
BOOTSTRAP_XID = 1
FROZEN_XID = 2

PG_EPOCH = dt.date(2000, 1, 1)


def maxalign(x: int, align: int = 8) -> int:
    return (x + align - 1) & ~(align - 1)


# ---------------------------------------------------------------- type support
class Attr:
    """One column, in the physical order it appears inside a tuple."""

    def __init__(self, name: str, typename: str, nullable: bool):
        self.name = name
        self.typename = typename
        self.nullable = nullable
        base = typename.split("(")[0].strip().lower()
        if base in ("integer", "int", "int4", "serial"):
            self.typlen, self.align, self.kind = 4, 4, "int"
        elif base in ("smallint", "int2", "smallserial"):
            self.typlen, self.align, self.kind = 2, 2, "int"
        elif base in ("bigint", "int8", "bigserial"):
            self.typlen, self.align, self.kind = 8, 8, "int"
        elif base in ("boolean", "bool"):
            self.typlen, self.align, self.kind = 1, 1, "bool"
        elif base == "date":
            self.typlen, self.align, self.kind = 4, 4, "date"
        elif base in ("real", "float4"):
            self.typlen, self.align, self.kind = 4, 4, "float4"
        elif base in ("double precision", "float8"):
            self.typlen, self.align, self.kind = 8, 8, "float8"
        elif base in ("text", "character varying", "varchar", "citext"):
            self.typlen, self.align, self.kind = -1, 4, "text"
        elif base in ("character", "char", "bpchar"):
            self.typlen, self.align, self.kind = -1, 4, "bpchar"
        else:
            raise SystemExit("column %r has unsupported type %r" % (name, typename))

    def decode(self, raw: bytes):
        if self.kind == "int":
            return int.from_bytes(raw, "little", signed=True)
        if self.kind == "bool":
            return raw[0] != 0
        if self.kind == "date":
            return PG_EPOCH + dt.timedelta(
                days=int.from_bytes(raw, "little", signed=True))
        if self.kind == "float4":
            return struct.unpack("<f", raw)[0]
        if self.kind == "float8":
            return struct.unpack("<d", raw)[0]
        if self.kind == "bpchar":
            return raw.decode("utf-8")
        return raw.decode("utf-8")


def read_varlena(buf: bytes, off: int):
    """Return (payload, total_bytes_consumed) for the varlena datum at `off`."""
    b = buf[off]
    if b == 0x01:                                  # VARATT_IS_1B_E - TOAST/expanded
        raise SystemExit(
            "tuple at offset %d holds an out-of-line (TOAST) datum; the task "
            "states every required value is inline" % off)
    if b & 0x01:                                   # VARATT_IS_1B - short header
        total = (b >> 1) & 0x7F
        return buf[off + 1: off + total], total
    header = struct.unpack_from("<I", buf, off)[0]
    if (header & 0x03) == 0x02:                    # VARATT_IS_4B_C - compressed
        raise SystemExit("tuple at offset %d holds a compressed datum" % off)
    total = (header >> 2) & 0x3FFFFFFF
    return buf[off + 4: off + total], total


# ---------------------------------------------------------------- tuple parsing
class Tuple:
    __slots__ = ("block", "lp", "lp_off", "lp_len", "xmin", "xmax", "field3",
                 "ctid", "infomask2", "infomask", "hoff", "natts", "values")


def parse_tuple(page: bytes, block: int, lp: int, off: int, length: int,
                attrs: list[Attr]) -> Tuple:
    if off + length > len(page) or length < 23:
        raise SystemExit("block %d line pointer %d is out of range (off=%d len=%d)"
                         % (block, lp, off, length))
    body = page[off: off + length]
    xmin, xmax, field3 = struct.unpack_from("<III", body, 0)
    ctid_blk_hi, ctid_blk_lo, ctid_off = struct.unpack_from("<HHH", body, 12)
    infomask2, infomask, hoff = struct.unpack_from("<HHB", body, 18)

    t = Tuple()
    t.block, t.lp, t.lp_off, t.lp_len = block, lp, off, length
    t.xmin, t.xmax, t.field3 = xmin, xmax, field3
    t.ctid = ((ctid_blk_hi << 16) | ctid_blk_lo, ctid_off)
    t.infomask2, t.infomask, t.hoff = infomask2, infomask, hoff
    t.natts = infomask2 & HEAP_NATTS_MASK

    if t.natts > len(attrs):
        raise SystemExit("block %d lp %d claims %d attributes, the schema has %d"
                         % (block, lp, t.natts, len(attrs)))

    nulls = None
    if infomask & HEAP_HASNULL:
        bitmap_len = (t.natts + 7) // 8
        if 23 + bitmap_len > hoff:
            raise SystemExit("block %d lp %d: null bitmap does not fit in t_hoff"
                             % (block, lp))
        nulls = body[23: 23 + bitmap_len]
    if hoff > length or hoff % 8:
        raise SystemExit("block %d lp %d has an implausible t_hoff %d"
                         % (block, lp, hoff))

    values = []
    cur = hoff
    for i, a in enumerate(attrs):
        if i >= t.natts:                      # attribute added after this tuple
            values.append(None)
            continue
        if nulls is not None and not (nulls[i >> 3] >> (i & 7)) & 1:
            values.append(None)               # NULL consumes no storage at all
            continue
        if a.typlen < 0:
            # att_align_pointer: a varlena with a non-zero leading byte is never
            # preceded by alignment padding, so the 1-byte-header case packs tight
            if body[cur] != 0:
                pass
            else:
                cur = maxalign(cur, a.align)
            payload, consumed = read_varlena(body, cur)
            values.append(a.decode(payload))
            cur += consumed
        else:
            cur = maxalign(cur, a.align)
            values.append(a.decode(body[cur: cur + a.typlen]))
            cur += a.typlen
        if cur > length:
            raise SystemExit("block %d lp %d: attribute %s runs past the tuple"
                             % (block, lp, a.name))
    t.values = values
    return t


def parse_page(page: bytes, block: int, attrs: list[Attr]):
    """Yield the tuples of one block plus a per-block line-pointer census."""
    if len(page) != 8192:
        raise SystemExit("block %d is %d bytes, expected 8192" % (block, len(page)))
    (pd_lsn_hi, pd_lsn_lo, pd_checksum, pd_flags, pd_lower, pd_upper,
     pd_special, pd_pagesize_version, pd_prune_xid) = struct.unpack_from(
        "<IIHHHHHHI", page, 0)

    census = {"LP_UNUSED": 0, "LP_NORMAL": 0, "LP_REDIRECT": 0, "LP_DEAD": 0}
    tuples = []
    if pd_lower == 0 and pd_upper == 0:
        return tuples, census, {"empty": True}          # never-initialised block

    page_size = pd_pagesize_version & 0xFF00
    layout_version = pd_pagesize_version & 0x00FF
    if page_size != 8192:
        raise SystemExit("block %d declares page size %d" % (block, page_size))
    if layout_version != 4:
        raise SystemExit("block %d has page layout version %d, expected 4 "
                         "(PostgreSQL 8.3-16)" % (block, layout_version))
    if not (PAGE_HEADER_SIZE <= pd_lower <= pd_upper <= pd_special <= 8192):
        raise SystemExit("block %d has an inconsistent page header "
                         "(lower=%d upper=%d special=%d)"
                         % (block, pd_lower, pd_upper, pd_special))

    n_line_pointers = (pd_lower - PAGE_HEADER_SIZE) // ITEM_ID_SIZE
    for i in range(n_line_pointers):
        raw = struct.unpack_from("<I", page, PAGE_HEADER_SIZE + i * ITEM_ID_SIZE)[0]
        lp_off = raw & 0x7FFF
        lp_flags = (raw >> 15) & 0x3
        lp_len = (raw >> 17) & 0x7FFF
        lp = i + 1                                   # OffsetNumber is 1-based
        if lp_flags == LP_UNUSED:
            census["LP_UNUSED"] += 1
        elif lp_flags == LP_REDIRECT:
            # A HOT redirect root holds no tuple: lp_off is the OffsetNumber of
            # the first live version, not a byte offset.  Parsing it as a tuple
            # would read arbitrary page bytes.
            census["LP_REDIRECT"] += 1
        elif lp_flags == LP_DEAD:
            census["LP_DEAD"] += 1                   # pruned; no storage remains
        else:
            census["LP_NORMAL"] += 1
            tuples.append(parse_tuple(page, block, lp, lp_off, lp_len, attrs))

    header = {"pd_lower": pd_lower, "pd_upper": pd_upper, "pd_special": pd_special,
              "pd_flags": pd_flags, "pd_prune_xid": pd_prune_xid,
              "layout_version": layout_version, "line_pointers": n_line_pointers,
              "lsn": (pd_lsn_hi, pd_lsn_lo), "checksum": pd_checksum}
    return tuples, census, header


# ---------------------------------------------------------------- visibility
class Snapshot:
    def __init__(self, xmin: int, xmax: int, xip):
        self.xmin, self.xmax, self.xip = xmin, xmax, set(xip)

    def in_progress(self, xid: int) -> bool:
        """XidInMVCCSnapshot: was `xid` still running when the snapshot was taken?

        Transactions at or above xmax had not finished (indeed, may not even have
        started); transactions below xmin had all finished; in between, only the
        explicit in-progress list decides.  A transaction that COMMITTED after
        the snapshot was taken is therefore still 'in progress' for this
        snapshot, no matter what its commit log entry says today."""
        if xid >= self.xmax:
            return True
        if xid < self.xmin:
            return False
        return xid in self.xip


class TxStatus:
    def __init__(self, mapping):
        self.map = mapping
        self.consulted = set()

    def __call__(self, xid: int) -> str:
        st = self.map.get(xid)
        if st is None:
            raise SystemExit("tx_status.csv has no entry for xid %d" % xid)
        self.consulted.add(xid)
        return st


def xmax_is_locked_only(infomask: int) -> bool:
    """HEAP_XMAX_IS_LOCKED_ONLY (htup_details.h).

    An xmax that records a row lock rather than a deletion.  The explicit
    HEAP_XMAX_LOCK_ONLY bit covers every lock mode on a modern page; the second
    arm keeps faith with the macro, which also treats a bare exclusive lock bit
    (no MultiXact, no key-share) as lock-only for tuples written by older
    servers."""
    if infomask & HEAP_XMAX_LOCK_ONLY:
        return True
    return (infomask & (HEAP_XMAX_IS_MULTI | HEAP_LOCK_MASK)) == HEAP_XMAX_EXCL_LOCK


def xmin_committed(t: Tuple, status: TxStatus, notes: dict) -> bool:
    if (t.infomask & HEAP_XMIN_FROZEN) == HEAP_XMIN_FROZEN or t.xmin == FROZEN_XID:
        notes["frozen"] += 1
        return True                       # frozen: committed and older than all
    if t.infomask & HEAP_XMIN_INVALID:
        return False                      # hint bit: the inserter aborted
    st = status(t.xmin)
    if t.infomask & HEAP_XMIN_COMMITTED and st != "committed":
        notes["hint_conflicts"] += 1
    return st == "committed"


def tuple_visible(t: Tuple, snap: Snapshot, status: TxStatus, notes: dict) -> bool:
    """HeapTupleSatisfiesMVCC, minus the cases this fixture excludes."""
    if t.infomask & HEAP_XMAX_IS_MULTI:
        raise SystemExit("block %d lp %d has a MultiXact xmax" % (t.block, t.lp))
    if t.infomask & HEAP_COMBOCID:
        raise SystemExit("block %d lp %d carries a combo command id"
                         % (t.block, t.lp))

    if not xmin_committed(t, status, notes):
        return False                      # inserter aborted or is still running
    if not ((t.infomask & HEAP_XMIN_FROZEN) == HEAP_XMIN_FROZEN
            or t.xmin == FROZEN_XID):
        if snap.in_progress(t.xmin):
            return False                  # inserted after this snapshot was taken

    if t.xmax != INVALID_XID and xmax_is_locked_only(t.infomask):
        notes["lock_only"] += 1           # a row lock, not a deletion

    # Same order as HeapTupleSatisfiesMVCC.
    if t.infomask & HEAP_XMAX_INVALID:
        return True                       # hint bit: xmax is aborted or unset
    if t.xmax == INVALID_XID:
        return True
    if xmax_is_locked_only(t.infomask):
        # SELECT ... FOR UPDATE / FOR SHARE / FOR KEY SHARE puts the locker's
        # xid in xmax.  The row is still live no matter what the commit log
        # says about that transaction, and PostgreSQL only stamps
        # HEAP_XMAX_INVALID over it if the page is later pruned - which never
        # happened for these pages.  Reading xmax without this test deletes
        # rows that were merely locked.
        return True

    st = status(t.xmax)
    if st != "committed":
        return True                       # the deleter aborted or is still running
    if snap.in_progress(t.xmax):
        return True                       # deleted after this snapshot was taken
    return False


# ---------------------------------------------------------------- driver
def load_schema(path: Path):
    js = json.loads(path.read_text(encoding="utf-8"))
    attrs = [Attr(c["name"], c["type"], c.get("nullable", True))
             for c in js["columns"]]
    snap = Snapshot(int(js["snapshot_xmin"]), int(js["snapshot_xmax"]),
                    [int(x) for x in js["snapshot_xip"]])
    block_size = int(js.get("block_size", 8192))
    if block_size != 8192:
        raise SystemExit("this recovery only implements 8192-byte blocks")
    return js, attrs, snap


def load_tx_status(path: Path) -> TxStatus:
    mapping = {}
    with path.open("r", encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            xid = int(row["xid"])
            st = row["status"].strip().lower().replace(" ", "_").replace("-", "_")
            if st not in ("committed", "aborted", "in_progress"):
                raise SystemExit("unknown transaction status %r for xid %d"
                                 % (row["status"], xid))
            mapping[xid] = st
    if not mapping:
        raise SystemExit("tx_status.csv is empty")
    return TxStatus(mapping)


def format_value(v) -> str:
    if v is None:
        return "\\N"
    if isinstance(v, bool):
        return "t" if v else "f"
    if isinstance(v, dt.date):
        return v.isoformat()
    return str(v)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--heap", default="/app/heap_pages.bin")
    ap.add_argument("--tx", default="/app/tx_status.csv")
    ap.add_argument("--schema", default="/app/table_schema.json")
    ap.add_argument("--out", default="/app/recovered.csv")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args(argv)

    js, attrs, snap = load_schema(Path(args.schema))
    status = load_tx_status(Path(args.tx))
    heap = Path(args.heap).read_bytes()

    if len(heap) % 8192:
        raise SystemExit("heap_pages.bin is %d bytes, not a multiple of 8192"
                         % len(heap))
    nblocks = len(heap) // 8192

    census = {"LP_UNUSED": 0, "LP_NORMAL": 0, "LP_REDIRECT": 0, "LP_DEAD": 0}
    notes = {"frozen": 0, "hint_conflicts": 0, "lock_only": 0}
    all_tuples = []
    for blk in range(nblocks):
        tuples, c, _hdr = parse_page(heap[blk * 8192:(blk + 1) * 8192], blk, attrs)
        for k in census:
            census[k] += c[k]
        all_tuples.extend(tuples)

    pk_names = js["primary_key"]
    colnames = [a.name for a in attrs]
    pk_idx = [colnames.index(n) for n in pk_names]

    visible = {}
    for t in all_tuples:
        if not tuple_visible(t, snap, status, notes):
            continue
        key = tuple(t.values[i] for i in pk_idx)
        if any(k is None for k in key):
            raise SystemExit("a visible tuple has a NULL primary key component")
        if key in visible:
            raise SystemExit(
                "two tuples are simultaneously visible for primary key %r "
                "(blocks %d/%d) - the snapshot does not define a unique state"
                % (key, visible[key].block, t.block))
        visible[key] = t

    out = Path(args.out)
    with out.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(colnames)
        for key in sorted(visible):
            w.writerow([format_value(v) for v in visible[key].values])

    if args.report:
        versions = {}
        for t in all_tuples:
            versions.setdefault(tuple(t.values[i] for i in pk_idx), []).append(t)
        multi = {k: len(v) for k, v in versions.items() if len(v) > 1}
        newest_xmin_wrong = 0
        for key, t in visible.items():
            if t.xmin != max(x.xmin for x in versions[key]):
                newest_xmin_wrong += 1
        print(json.dumps({
            "blocks": nblocks,
            "line_pointers": census,
            "physical_tuples": len(all_tuples),
            "distinct_primary_keys_on_disk": len(versions),
            "keys_with_several_physical_versions": len(multi),
            "visible_rows": len(visible),
            "snapshot": {"xmin": snap.xmin, "xmax": snap.xmax,
                         "xip": sorted(snap.xip)},
            "transaction_states_used": len(status.consulted),
            "keys_whose_visible_version_is_not_the_newest_xmin": newest_xmin_wrong,
            "tuples_kept_despite_xmax_lock_only": notes["lock_only"],
            "frozen_tuples_seen": notes["frozen"],
            "hint_bit_conflicts_with_tx_status": notes["hint_conflicts"],
            "output": str(out),
        }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
