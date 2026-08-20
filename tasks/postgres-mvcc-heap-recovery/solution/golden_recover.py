#!/usr/bin/env python3
"""Reference recovery of the MVCC-visible rows of a PostgreSQL 16 heap.

Reads only what the solver is given - the raw relation blocks, the cluster's
pg_xact and pg_subtrans segments, and the schema/snapshot description - and
reconstructs the logical table as PostgreSQL would have seen it under the
supplied snapshot.

    golden_recover.py [--heap /app/heap_pages.bin] [--pg-xact /app/pg_xact]
                      [--pg-subtrans /app/pg_subtrans]
                      [--pg-multixact /app/pg_multixact]
                      [--schema /app/table_schema.json] [--out /app/recovered.csv]
                      [--report]

Transaction state comes from the cluster's own SLRU segments: `pg_xact` is the
commit log (two bits per xid) and `pg_subtrans` maps a subtransaction xid to its
immediate parent.  Both matter.  `pg_current_snapshot()` exports only *top-level*
xids in its xip list, so a tuple written by a subtransaction of a transaction
that was still running carries a subxid that reads COMMITTED in the commit log
and is absent from snapshot_xip; only the subtransaction map shows that its
topmost parent was in flight.

When several transactions lock one row, or a locker coexists with an updater,
t_xmax holds a MultiXactId instead of a transaction id, flagged by
HEAP_XMAX_IS_MULTI.  The mxid resolves through pg_multixact/offsets (start of
its member list; the next multi's entry bounds it) and pg_multixact/members
(member xids plus a status flag each).  Locker members never delete the tuple;
the at-most-one update member deletes it exactly when that transaction committed
and is outside the snapshot - resolved through pg_subtrans like any other xid.

No hidden expected answer is consulted and no row is hard-coded anywhere in this
file.  Structure references: src/include/storage/bufpage.h (PageHeaderData,
ItemIdData), src/include/access/htup_details.h (HeapTupleHeaderData, t_infomask
bits, att_align/att_addlength), src/include/access/clog.h and
src/backend/access/transam/subtrans.c (SLRU geometry), and
src/backend/utils/time/snapmgr.c plus heapam_visibility.c
(HeapTupleSatisfiesMVCC, XidInMVCCSnapshot, TransactionIdDidCommit,
SubTransGetTopmostTransaction).
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

# ---------------------------------------------------------------- SLRU layout
BLCKSZ = 8192
SLRU_PAGES_PER_SEGMENT = 32
CLOG_BITS_PER_XACT = 2
CLOG_XACTS_PER_BYTE = 4
CLOG_XACTS_PER_PAGE = BLCKSZ * CLOG_XACTS_PER_BYTE          # 32768
CLOG_XACT_BITMASK = (1 << CLOG_BITS_PER_XACT) - 1
SUBTRANS_XACTS_PER_PAGE = BLCKSZ // 4                       # 2048

# pg_multixact geometry (src/backend/access/transam/multixact.c)
MULTIXACT_OFFSETS_PER_PAGE = BLCKSZ // 4                    # 2048
MULTIXACT_MEMBERS_PER_GROUP = 4
MULTIXACT_MEMBERGROUP_SIZE = 4 + 4 * 4                      # 4 flag bytes + 4 xids
MULTIXACT_GROUPS_PER_PAGE = BLCKSZ // MULTIXACT_MEMBERGROUP_SIZE   # 409
MULTIXACT_MEMBERS_PER_PAGE = MULTIXACT_GROUPS_PER_PAGE * MULTIXACT_MEMBERS_PER_GROUP
# MultiXactStatus: 0 ForKeyShare, 1 ForShare, 2 ForNoKeyUpdate, 3 ForUpdate are
# LOCKERS; 4 NoKeyUpdate, 5 Update are UPDATERS (ISUPDATE_from_mxstatus)
MXS_IS_UPDATE = (4, 5)

XACT_IN_PROGRESS = 0x00
XACT_COMMITTED = 0x01
XACT_ABORTED = 0x02
XACT_SUB_COMMITTED = 0x03

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


class MultiXactLog:
    """pg_multixact/offsets and pg_multixact/members, read from the raw SLRU
    segment files.

    offsets holds one 32-bit member index per MultiXactId (2048 per page); the
    member list of multi M spans [offsets[M], offsets[M+1]).  Member index 0 is
    reserved so that a zero entry can mean 'never written', which is also why
    the list of the newest multi on disk can only be bounded if one more multi
    was created after it.  members packs groups of four: four status-flag bytes
    followed by four 32-bit xids (20 bytes per group, 409 groups per page)."""

    def __init__(self, root: Path):
        self.offsets = TransactionLog._load(root / "offsets",
                                            "pg_multixact/offsets")
        self.member_segs = TransactionLog._load(root / "members",
                                                "pg_multixact/members")
        self.resolved = {}

    def _offset(self, mxid: int) -> int:
        pageno = mxid // MULTIXACT_OFFSETS_PER_PAGE
        segno, page = divmod(pageno, SLRU_PAGES_PER_SEGMENT)
        blob = self.offsets.get(segno)
        if blob is None:
            raise SystemExit("pg_multixact/offsets has no segment %04X, needed "
                             "for multi %d" % (segno, mxid))
        off = page * BLCKSZ + (mxid % MULTIXACT_OFFSETS_PER_PAGE) * 4
        return struct.unpack_from("<I", blob, off)[0]

    def members(self, mxid: int):
        """[(xid, status)] for one multi."""
        if mxid in self.resolved:
            return self.resolved[mxid]
        start, end = self._offset(mxid), self._offset(mxid + 1)
        if start == 0:
            raise SystemExit("multi %d was never created (offsets entry is 0)"
                             % mxid)
        if end == 0 or end < start or end - start > 64:
            raise SystemExit("cannot bound the member list of multi %d "
                             "(offsets[%d]=%d, offsets[%d]=%d)"
                             % (mxid, mxid, start, mxid + 1, end))
        out = []
        for i in range(start, end):
            pageno, within = divmod(i, MULTIXACT_MEMBERS_PER_PAGE)
            segno, page = divmod(pageno, SLRU_PAGES_PER_SEGMENT)
            blob = self.member_segs.get(segno)
            if blob is None:
                raise SystemExit("pg_multixact/members has no segment %04X"
                                 % segno)
            group, idx = divmod(within, MULTIXACT_MEMBERS_PER_GROUP)
            base = page * BLCKSZ + group * MULTIXACT_MEMBERGROUP_SIZE
            flag = blob[base + idx]
            xid = struct.unpack_from("<I", blob, base + 4 + idx * 4)[0]
            if xid == INVALID_XID:
                raise SystemExit("multi %d member %d decodes to xid 0" % (mxid, i))
            out.append((xid, flag))
        self.resolved[mxid] = out
        return out

    def updater(self, mxid: int):
        """The xid of the at-most-one update member, or None (pure lock)."""
        ups = [x for x, f in self.members(mxid) if f in MXS_IS_UPDATE]
        if len(ups) > 1:
            raise SystemExit("multi %d has %d update members" % (mxid, len(ups)))
        return ups[0] if ups else None


# ---------------------------------------------------------------- visibility
class Snapshot:
    def __init__(self, xmin: int, xmax: int, xip):
        self.xmin, self.xmax, self.xip = xmin, xmax, set(xip)

    def in_progress(self, xid: int, log=None) -> bool:
        """XidInMVCCSnapshot: was `xid` still running when the snapshot was taken?

        Transactions at or above xmax had not finished (indeed, may not even have
        started); transactions below xmin had all finished; in between, the
        explicit in-progress list decides.  A transaction that COMMITTED after
        the snapshot was taken is therefore still 'in progress' for this
        snapshot, no matter what its commit log entry says today.

        The list holds **top-level** xids only.  A tuple stamped by a
        subtransaction therefore has to be resolved through pg_subtrans first,
        or it will look like an unrelated transaction that had already
        finished."""
        if xid >= self.xmax:
            return True
        if xid < self.xmin:
            return False
        if xid in self.xip:
            return True
        if log is None:
            return False
        top = log.topmost(xid)
        if top == xid:
            return False
        if top >= self.xmax:
            return True
        if top < self.xmin:
            return False
        return top in self.xip


class TransactionLog:
    """The cluster's commit log and subtransaction map, read straight from the
    SLRU segment files.

    pg_xact stores two bits per transaction id; pg_subtrans stores a four-byte
    parent transaction id per entry, zero for a top-level transaction.  Segments
    hold SLRU_PAGES_PER_SEGMENT pages and are named with the segment number in
    four hex digits."""

    def __init__(self, clog_dir: Path, subtrans_dir: Path):
        self.clog = self._load(clog_dir, "pg_xact")
        self.subtrans = self._load(subtrans_dir, "pg_subtrans")
        self.multi = None              # MultiXactLog, attached by the loader
        self.consulted = set()
        self.subtrans_lookups = 0
        self.resolved_subxids = {}

    @staticmethod
    def _load(directory: Path, label: str) -> dict:
        if not directory.is_dir():
            raise SystemExit("%s: %s is not a directory" % (label, directory))
        out = {}
        for f in sorted(directory.iterdir()):
            if not f.is_file():
                continue
            name = f.name.upper()
            if len(name) < 4 or any(c not in "0123456789ABCDEF" for c in name):
                continue                      # not an SLRU segment
            blob = f.read_bytes()
            if len(blob) % BLCKSZ:
                raise SystemExit("%s/%s is %d bytes, not a multiple of %d"
                                 % (label, f.name, len(blob), BLCKSZ))
            out[int(name, 16)] = blob
        if not out:
            raise SystemExit("%s holds no SLRU segment files" % label)
        return out

    # -------------------------------------------------------------- pg_xact
    def raw_status(self, xid: int) -> int:
        pageno = xid // CLOG_XACTS_PER_PAGE
        segno, page = divmod(pageno, SLRU_PAGES_PER_SEGMENT)
        blob = self.clog.get(segno)
        if blob is None:
            raise SystemExit("pg_xact has no segment %04X, needed for xid %d"
                             % (segno, xid))
        off = page * BLCKSZ + (xid % CLOG_XACTS_PER_PAGE) // CLOG_XACTS_PER_BYTE
        if off >= len(blob):
            raise SystemExit("pg_xact segment %04X is too short for xid %d"
                             % (segno, xid))
        shift = (xid % CLOG_XACTS_PER_BYTE) * CLOG_BITS_PER_XACT
        return (blob[off] >> shift) & CLOG_XACT_BITMASK

    # ----------------------------------------------------------- pg_subtrans
    def parent(self, xid: int) -> int:
        pageno = xid // SUBTRANS_XACTS_PER_PAGE
        segno, page = divmod(pageno, SLRU_PAGES_PER_SEGMENT)
        blob = self.subtrans.get(segno)
        if blob is None:
            return INVALID_XID              # never written: a top-level xid
        off = page * BLCKSZ + (xid % SUBTRANS_XACTS_PER_PAGE) * 4
        if off + 4 > len(blob):
            return INVALID_XID
        self.subtrans_lookups += 1
        return struct.unpack_from("<I", blob, off)[0]

    def topmost(self, xid: int) -> int:
        """SubTransGetTopmostTransaction: walk parents until a top-level xid.

        Savepoints nest, so pg_subtrans records the *immediate* parent and the
        walk can take several hops.  A parent id is always lower than its child,
        which bounds the loop."""
        seen = set()
        cur = xid
        while True:
            parent = self.parent(cur)
            if parent == INVALID_XID or parent >= cur or parent in seen:
                if cur != xid:
                    self.resolved_subxids[xid] = cur
                return cur
            seen.add(cur)
            cur = parent

    def is_subtransaction(self, xid: int) -> bool:
        return self.parent(xid) != INVALID_XID

    # --------------------------------------------------------- commit state
    def state(self, xid: int) -> str:
        """committed / aborted / in_progress, as TransactionIdDidCommit sees it.

        A SUB_COMMITTED entry means the subtransaction finished but its parent
        had not yet, so the parent decides."""
        self.consulted.add(xid)
        seen = set()
        cur = xid
        while True:
            st = self.raw_status(cur)
            if st == XACT_COMMITTED:
                return "committed"
            if st == XACT_ABORTED:
                return "aborted"
            if st == XACT_IN_PROGRESS:
                return "in_progress"
            parent = self.parent(cur)          # XACT_SUB_COMMITTED
            if parent == INVALID_XID or parent in seen:
                return "in_progress"
            seen.add(cur)
            cur = parent

    def __call__(self, xid: int) -> str:
        return self.state(xid)


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


def xmin_committed(t: Tuple, status, notes: dict) -> bool:
    if (t.infomask & HEAP_XMIN_FROZEN) == HEAP_XMIN_FROZEN or t.xmin == FROZEN_XID:
        notes["frozen"] += 1
        return True                       # frozen: committed and older than all
    if t.infomask & HEAP_XMIN_INVALID:
        return False                      # hint bit: the inserter aborted
    st = status(t.xmin)
    if t.infomask & HEAP_XMIN_COMMITTED and st != "committed":
        notes["hint_conflicts"] += 1
    return st == "committed"


def tuple_visible(t: Tuple, snap: Snapshot, status, notes: dict) -> bool:
    """HeapTupleSatisfiesMVCC, minus the cases this fixture excludes."""
    if t.infomask & HEAP_COMBOCID:
        raise SystemExit("block %d lp %d carries a combo command id"
                         % (t.block, t.lp))

    if not xmin_committed(t, status, notes):
        return False                      # inserter aborted or is still running
    if not ((t.infomask & HEAP_XMIN_FROZEN) == HEAP_XMIN_FROZEN
            or t.xmin == FROZEN_XID):
        if snap.in_progress(t.xmin, status):
            return False                  # inserted after this snapshot was taken

    # Same order as HeapTupleSatisfiesMVCC.
    if t.infomask & HEAP_XMAX_INVALID:
        return True                       # hint bit: xmax is aborted or unset
    if t.xmax == INVALID_XID:
        return True

    if t.infomask & HEAP_XMAX_IS_MULTI:
        # xmax is a MultiXactId, NOT a transaction id.  In a fresh cluster the
        # mxids are small integers that collide with committed bootstrap xids,
        # so misreading this field silently deletes live rows.
        notes["multixact"] = notes.get("multixact", 0) + 1
        if t.infomask & HEAP_XMAX_LOCK_ONLY:
            notes["multi_lock_only"] = notes.get("multi_lock_only", 0) + 1
            return True                   # every member is a locker
        if status.multi is None:
            raise SystemExit(
                "block %d lp %d has a MultiXact xmax but no pg_multixact "
                "evidence was supplied" % (t.block, t.lp))
        u = status.multi.updater(t.xmax)
        if u is None:
            notes["multi_lock_only"] = notes.get("multi_lock_only", 0) + 1
            return True                   # defensive: no update member after all
        notes["multi_updater"] = notes.get("multi_updater", 0) + 1
        if status(u) != "committed":
            return True                   # the updater aborted or is running
        if snap.in_progress(u, status):
            notes["multi_updater_snapshot"] = \
                notes.get("multi_updater_snapshot", 0) + 1
            return True                   # updater finished after the snapshot
        return False

    if xmax_is_locked_only(t.infomask):
        notes["lock_only"] += 1           # a row lock, not a deletion
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
    if snap.in_progress(t.xmax, status):
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


def load_transaction_log(clog_dir: Path, subtrans_dir: Path,
                         multixact_dir: Path = None) -> TransactionLog:
    log = TransactionLog(clog_dir, subtrans_dir)
    if multixact_dir is not None:
        log.multi = MultiXactLog(Path(multixact_dir))
    return log


def _chain_depth(log: "TransactionLog", xid: int) -> int:
    depth, cur = 0, xid
    while True:
        parent = log.parent(cur)
        if parent == INVALID_XID or parent >= cur:
            return depth
        depth += 1
        cur = parent


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
    ap.add_argument("--pg-xact", dest="pg_xact", default="/app/pg_xact")
    ap.add_argument("--pg-subtrans", dest="pg_subtrans", default="/app/pg_subtrans")
    ap.add_argument("--pg-multixact", dest="pg_multixact",
                    default="/app/pg_multixact")
    ap.add_argument("--schema", default="/app/table_schema.json")
    ap.add_argument("--out", default="/app/recovered.csv")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args(argv)

    js, attrs, snap = load_schema(Path(args.schema))
    status = load_transaction_log(Path(args.pg_xact), Path(args.pg_subtrans),
                                  Path(args.pg_multixact))
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
            "commit_log_segments": sorted("%04X" % k for k in status.clog),
            "subtransaction_map_segments": sorted("%04X" % k
                                                  for k in status.subtrans),
            "subtransaction_xids_resolved": len(status.resolved_subxids),
            "subtransaction_parent_lookups": status.subtrans_lookups,
            "multixact_offsets_segments": sorted(
                "%04X" % k for k in status.multi.offsets),
            "multixact_members_segments": sorted(
                "%04X" % k for k in status.multi.member_segs),
            "multixacts_resolved": sorted(status.multi.resolved),
            "tuples_with_multixact_xmax": notes.get("multixact", 0),
            "multixact_lock_only_tuples": notes.get("multi_lock_only", 0),
            "multixact_updater_tuples": notes.get("multi_updater", 0),
            "multixact_updater_saved_by_snapshot":
                notes.get("multi_updater_snapshot", 0),
            "frozen_tuples_seen_in_notes": notes.get("frozen", 0),
            "deepest_subtransaction_chain": max(
                [_chain_depth(status, x) for x in status.resolved_subxids] or [0]),
            "keys_whose_visible_version_is_not_the_newest_xmin": newest_xmin_wrong,
            "tuples_kept_despite_xmax_lock_only": notes["lock_only"],
            "frozen_tuples_seen": notes["frozen"],
            "hint_bit_conflicts_with_commit_log": notes["hint_conflicts"],
            "output": str(out),
        }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
