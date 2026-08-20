#!/usr/bin/env python3
"""Reference recovery for the v5 lost-clog-tail PostgreSQL 16 incident.

Reads only what the solver is given - the raw heap files of three relations,
the surviving pg_xact and pg_subtrans SLRU segments, and the schema/snapshot
description - and reconstructs the rows of the output relation exactly as
PostgreSQL saw them under the supplied snapshot.

    golden_recover.py [--dir /app] [--schema /app/table_schema.json]
                      [--out /app/recovered.csv] [--report]

The surviving commit log does not record an outcome for every transaction the
pages reference: the cluster's WAL volume was lost, and pg_xact writeback is
lazy, so the bits for the most recent transactions were never flushed.  Those
entries read zero (nominally "in progress"), which the supplied snapshot
refutes for any xid below snapshot_xmin - or inside [xmin, xmax) and absent
from snapshot_xip - because such a transaction had already completed when the
snapshot was taken.  Each such xid therefore committed or aborted, and the
file cannot say which.

This oracle resolves them by GLOBAL INFERENCE rather than any per-file rule:

  1. every tuple hint bit PostgreSQL genuinely stamped is authoritative
     evidence about one transaction's outcome (and, by atomicity, about every
     tuple of that transaction in every relation);
  2. all remaining assignments of {committed, aborted} to the unresolved
     transactions are enumerated exhaustively;
  3. for each candidate assignment the full MVCC-visible state of ALL THREE
     relations is computed under the supplied snapshot, and the candidate is
     rejected if that state violates any declared PRIMARY KEY / UNIQUE,
     FOREIGN KEY, NOT NULL or CHECK constraint, or contradicts any hint bit;
  4. exactly one assignment must survive; its accounts state is the answer.

No hidden expected answer is consulted and no row or outcome is hard-coded.
Structure references: bufpage.h, htup_details.h, heapam_visibility.c
(HeapTupleSatisfiesMVCC, XidInMVCCSnapshot, SetHintBits), clog.c/subtrans.c
(SLRU geometry, lazy writeback), transam/README (WAL closes the clog gap
during crash recovery - which is exactly what a lost WAL volume prevents).
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import itertools
import json
import re
import struct
import sys
from pathlib import Path

# ---------------------------------------------------------------- page layout
PAGE_HEADER_SIZE = 24
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
FROZEN_XID = 2

# ---------------------------------------------------------------- SLRU layout
BLCKSZ = 8192
SLRU_PAGES_PER_SEGMENT = 32
CLOG_XACTS_PER_BYTE = 4
CLOG_XACTS_PER_PAGE = BLCKSZ * CLOG_XACTS_PER_BYTE
SUBTRANS_XACTS_PER_PAGE = BLCKSZ // 4

XACT_IN_PROGRESS = 0x00
XACT_COMMITTED = 0x01
XACT_ABORTED = 0x02
XACT_SUB_COMMITTED = 0x03

PG_EPOCH = dt.date(2000, 1, 1)


def maxalign(x: int, align: int = 8) -> int:
    return (x + align - 1) & ~(align - 1)


# ---------------------------------------------------------------- type support
class Attr:
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
        elif base in ("text", "character varying", "varchar"):
            self.typlen, self.align, self.kind = -1, 4, "text"
        else:
            raise SystemExit("column %r has unsupported type %r"
                             % (name, typename))

    def decode(self, raw: bytes):
        if self.kind == "int":
            return int.from_bytes(raw, "little", signed=True)
        if self.kind == "bool":
            return raw[0] != 0
        if self.kind == "date":
            return PG_EPOCH + dt.timedelta(
                days=int.from_bytes(raw, "little", signed=True))
        return raw.decode("utf-8")


def read_varlena(buf: bytes, off: int):
    b = buf[off]
    if b == 0x01:
        raise SystemExit("out-of-line TOAST datum at offset %d; the task "
                         "states every value is inline" % off)
    if b & 0x01:
        total = (b >> 1) & 0x7F
        return buf[off + 1: off + total], total
    header = struct.unpack_from("<I", buf, off)[0]
    if (header & 0x03) == 0x02:
        raise SystemExit("compressed datum at offset %d" % off)
    total = (header >> 2) & 0x3FFFFFFF
    return buf[off + 4: off + total], total


# ---------------------------------------------------------------- tuple parsing
class Tuple:
    __slots__ = ("rel", "block", "lp", "xmin", "xmax", "ctid", "infomask2",
                 "infomask", "hoff", "natts", "values")


def parse_tuple(page, rel, block, lp, off, length, attrs):
    if off + length > len(page) or length < 23:
        raise SystemExit("%s block %d lp %d out of range" % (rel, block, lp))
    body = page[off: off + length]
    xmin, xmax, _field3 = struct.unpack_from("<III", body, 0)
    b_hi, b_lo, c_off = struct.unpack_from("<HHH", body, 12)
    infomask2, infomask, hoff = struct.unpack_from("<HHB", body, 18)

    t = Tuple()
    t.rel, t.block, t.lp = rel, block, lp
    t.xmin, t.xmax = xmin, xmax
    t.ctid = ((b_hi << 16) | b_lo, c_off)
    t.infomask2, t.infomask, t.hoff = infomask2, infomask, hoff
    t.natts = infomask2 & HEAP_NATTS_MASK
    if t.natts > len(attrs):
        raise SystemExit("%s block %d lp %d claims %d attrs"
                         % (rel, block, lp, t.natts))

    nulls = None
    if infomask & HEAP_HASNULL:
        blen = (t.natts + 7) // 8
        if 23 + blen > hoff:
            raise SystemExit("null bitmap does not fit t_hoff")
        nulls = body[23: 23 + blen]
    if hoff > length or hoff % 8:
        raise SystemExit("implausible t_hoff %d" % hoff)

    values = []
    cur = hoff
    for i, a in enumerate(attrs):
        if i >= t.natts:
            values.append(None)
            continue
        if nulls is not None and not (nulls[i >> 3] >> (i & 7)) & 1:
            values.append(None)
            continue
        if a.typlen < 0:
            if body[cur] == 0:
                cur = maxalign(cur, a.align)
            payload, used = read_varlena(body, cur)
            values.append(a.decode(payload))
            cur += used
        else:
            cur = maxalign(cur, a.align)
            values.append(a.decode(body[cur: cur + a.typlen]))
            cur += a.typlen
        if cur > length:
            raise SystemExit("attribute %s runs past the tuple" % a.name)
    t.values = values
    return t


def parse_heap(data: bytes, rel: str, attrs):
    if len(data) % BLCKSZ:
        raise SystemExit("%s heap is not whole 8192-byte blocks" % rel)
    tuples = []
    census = {"LP_UNUSED": 0, "LP_NORMAL": 0, "LP_REDIRECT": 0, "LP_DEAD": 0}
    for blk in range(len(data) // BLCKSZ):
        page = data[blk * BLCKSZ:(blk + 1) * BLCKSZ]
        (lower, upper, special, pv) = struct.unpack_from("<HHHH", page, 12)
        if lower == 0 and upper == 0:
            continue
        if pv & 0xFF00 != BLCKSZ or pv & 0xFF != 4:
            raise SystemExit("%s block %d is not a v4-layout 8192 page"
                             % (rel, blk))
        if not (PAGE_HEADER_SIZE <= lower <= upper <= special <= BLCKSZ):
            raise SystemExit("%s block %d header inconsistent" % (rel, blk))
        n = (lower - PAGE_HEADER_SIZE) // ITEM_ID_SIZE
        for i in range(n):
            raw = struct.unpack_from("<I", page,
                                     PAGE_HEADER_SIZE + i * ITEM_ID_SIZE)[0]
            lp_off, lp_flags, lp_len = raw & 0x7FFF, (raw >> 15) & 3, \
                (raw >> 17) & 0x7FFF
            if lp_flags == LP_UNUSED:
                census["LP_UNUSED"] += 1
            elif lp_flags == LP_REDIRECT:
                census["LP_REDIRECT"] += 1
            elif lp_flags == LP_DEAD:
                census["LP_DEAD"] += 1
            else:
                census["LP_NORMAL"] += 1
                tuples.append(parse_tuple(page, rel, blk, i + 1, lp_off,
                                          lp_len, attrs))
    return tuples, census


# ---------------------------------------------------------------- SLRU logs
class TransactionLog:
    """Surviving pg_xact + pg_subtrans, read from the raw segment files."""

    def __init__(self, clog_dir: Path, subtrans_dir: Path):
        self.clog = self._load(clog_dir, "pg_xact")
        self.subtrans = self._load(subtrans_dir, "pg_subtrans")

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
                continue
            blob = f.read_bytes()
            if len(blob) % BLCKSZ:
                raise SystemExit("%s/%s is not whole SLRU pages"
                                 % (label, f.name))
            out[int(name, 16)] = blob
        if not out:
            raise SystemExit("%s holds no SLRU segments" % label)
        return out

    def raw_status(self, xid: int):
        """The two clog bits, or None when the file does not extend that far
        (equally unrecorded)."""
        pageno = xid // CLOG_XACTS_PER_PAGE
        segno, page = divmod(pageno, SLRU_PAGES_PER_SEGMENT)
        blob = self.clog.get(segno)
        if blob is None:
            return None
        off = page * BLCKSZ + (xid % CLOG_XACTS_PER_PAGE) // CLOG_XACTS_PER_BYTE
        if off >= len(blob):
            return None
        return (blob[off] >> ((xid % CLOG_XACTS_PER_BYTE) * 2)) & 3

    def parent(self, xid: int) -> int:
        pageno = xid // SUBTRANS_XACTS_PER_PAGE
        segno, page = divmod(pageno, SLRU_PAGES_PER_SEGMENT)
        blob = self.subtrans.get(segno)
        if blob is None:
            return INVALID_XID
        off = page * BLCKSZ + (xid % SUBTRANS_XACTS_PER_PAGE) * 4
        if off + 4 > len(blob):
            return INVALID_XID
        return struct.unpack_from("<I", blob, off)[0]

    def topmost(self, xid: int) -> int:
        seen = set()
        cur = xid
        while True:
            p = self.parent(cur)
            if p == INVALID_XID or p >= cur or p in seen:
                return cur
            seen.add(cur)
            cur = p

    def recorded_state(self, xid: int):
        """committed / aborted when the surviving file records it; None when
        the bits are zero or beyond the file (unrecorded)."""
        seen = set()
        cur = xid
        while True:
            st = self.raw_status(cur)
            if st is None or st == XACT_IN_PROGRESS:
                return None
            if st == XACT_COMMITTED:
                return "committed"
            if st == XACT_ABORTED:
                return "aborted"
            p = self.parent(cur)                # SUB_COMMITTED: parent decides
            if p == INVALID_XID or p in seen:
                return None
            seen.add(cur)
            cur = p


class Snapshot:
    def __init__(self, xmin, xmax, xip):
        self.xmin, self.xmax, self.xip = xmin, xmax, set(xip)

    def in_progress(self, xid: int, log: TransactionLog) -> bool:
        if xid >= self.xmax:
            return True
        if xid < self.xmin:
            return False
        if xid in self.xip:
            return True
        top = log.topmost(xid)
        if top == xid:
            return False
        if top >= self.xmax:
            return True
        if top < self.xmin:
            return False
        return top in self.xip

    def completed(self, xid: int, log: TransactionLog) -> bool:
        """The snapshot itself proves this xid had finished (committed or
        aborted) when the snapshot was taken."""
        return not self.in_progress(xid, log)


def xmax_is_locked_only(infomask: int) -> bool:
    if infomask & HEAP_XMAX_LOCK_ONLY:
        return True
    return (infomask & (HEAP_XMAX_IS_MULTI | HEAP_LOCK_MASK)) \
        == HEAP_XMAX_EXCL_LOCK


# ---------------------------------------------------------------- relations
class Relation:
    def __init__(self, meta: dict, base_dir: Path):
        self.name = meta["name"]
        self.attrs = [Attr(c["name"], c["type"], c["nullable"])
                      for c in meta["columns"]]
        self.colnames = [a.name for a in self.attrs]
        self.pk = meta["primary_key"]
        self.pk_idx = [self.colnames.index(c) for c in self.pk]
        self.checks = meta.get("checks", [])
        self.fks = meta.get("foreign_keys", [])
        data = (base_dir / meta["file"]).read_bytes()
        self.tuples, self.census = parse_heap(data, self.name, self.attrs)
        self.heap_bytes = len(data)

    def key_of(self, t: Tuple):
        return tuple(t.values[i] for i in self.pk_idx)


CHECK_RE = re.compile(r"^\s*(\w+)\s*(<>|!=|>=|<=|>|<|=)\s*(-?\d+)\s*$")


def check_fn(expr: str, colnames):
    m = CHECK_RE.match(expr)
    if not m:
        raise SystemExit("unsupported CHECK expression %r" % expr)
    col, op, lit = m.group(1), m.group(2), int(m.group(3))
    idx = colnames.index(col)
    ops = {"<>": lambda v: v != lit, "!=": lambda v: v != lit,
           ">": lambda v: v > lit, "<": lambda v: v < lit,
           ">=": lambda v: v >= lit, "<=": lambda v: v <= lit,
           "=": lambda v: v == lit}[op]
    return lambda row: row[idx] is None or ops(row[idx])


# ---------------------------------------------------------------- inference
class Evidence:
    """Everything the artifacts say about transaction outcomes."""

    def __init__(self, relations, log: TransactionLog, snap: Snapshot):
        self.log, self.snap = log, snap
        self.recorded = {}       # xid -> committed/aborted from surviving clog
        self.unknown = set()     # completed at the snapshot, outcome unrecorded
        self.irrelevant = set()  # unrecorded but in-progress at the snapshot
        self.hints = {}          # xid -> committed/aborted from hint bits
        self.hint_sources = {}

        xids = set()
        for rel in relations:
            for t in rel.tuples:
                if t.infomask & HEAP_XMAX_IS_MULTI:
                    raise SystemExit("MultiXact xmax at %s (%d,%d): out of "
                                     "scope for this fixture"
                                     % (t.rel, t.block, t.lp))
                if t.infomask & HEAP_COMBOCID:
                    raise SystemExit("combo CID present")
                xids.add(t.xmin)
                if t.xmax:
                    xids.add(t.xmax)
        xids.discard(INVALID_XID)

        for x in sorted(xids):
            st = log.recorded_state(x)
            if st is not None:
                self.recorded[x] = st
            elif snap.completed(x, log):
                # zero clog bits, yet the snapshot proves the transaction had
                # completed: the surviving commit log is stale for this xid
                self.unknown.add(x)
            else:
                self.irrelevant.add(x)

        # authoritative hint bits -> outcome facts (atomicity: one fact per
        # xid, regardless of which relation carries the stamp)
        def note(xid, outcome, src):
            if xid in self.recorded:
                if self.recorded[xid] != outcome:
                    raise SystemExit("hint %s contradicts recorded clog for "
                                     "xid %d" % (src, xid))
                return
            prev = self.hints.get(xid)
            if prev is not None and prev != outcome:
                raise SystemExit("contradictory hints for xid %d" % xid)
            self.hints[xid] = outcome
            self.hint_sources.setdefault(xid, []).append(src)

        for rel in relations:
            for t in rel.tuples:
                m = t.infomask
                if (m & HEAP_XMIN_FROZEN) == HEAP_XMIN_FROZEN:
                    pass                        # frozen: predates everything
                elif m & HEAP_XMIN_COMMITTED:
                    note(t.xmin, "committed",
                         "%s(%d,%d).xmin" % (t.rel, t.block, t.lp))
                elif m & HEAP_XMIN_INVALID:
                    note(t.xmin, "aborted",
                         "%s(%d,%d).xmin" % (t.rel, t.block, t.lp))
                if t.xmax and not xmax_is_locked_only(m):
                    if m & HEAP_XMAX_COMMITTED:
                        note(t.xmax, "committed",
                             "%s(%d,%d).xmax" % (t.rel, t.block, t.lp))
                    elif m & HEAP_XMAX_INVALID:
                        note(t.xmax, "aborted",
                             "%s(%d,%d).xmax" % (t.rel, t.block, t.lp))


def outcome_fn(evidence, assignment):
    """xid -> committed/aborted/None(in-progress) for one candidate."""
    rec, irr = evidence.recorded, evidence.irrelevant

    def fn(xid):
        st = rec.get(xid)
        if st is not None:
            return st
        if xid in assignment:
            return assignment[xid]
        if xid in irr:
            return None                       # in progress at the snapshot
        return None
    return fn


def tuple_visible(t: Tuple, snap: Snapshot, log, outcome) -> bool:
    """HeapTupleSatisfiesMVCC under a candidate outcome function."""
    m = t.infomask
    if (m & HEAP_XMIN_FROZEN) != HEAP_XMIN_FROZEN and t.xmin != FROZEN_XID:
        if outcome(t.xmin) != "committed":
            return False
        if snap.in_progress(t.xmin, log):
            return False
    if m & HEAP_XMAX_INVALID:
        return True
    if t.xmax == INVALID_XID:
        return True
    if xmax_is_locked_only(m):
        return True
    if outcome(t.xmax) != "committed":
        return True
    if snap.in_progress(t.xmax, log):
        return True
    return False


class Engine:
    """Enumerate candidate assignments; keep those whose full visible state
    satisfies every constraint and every hint."""

    def __init__(self, relations, log, snap, evidence):
        self.relations = relations
        self.log, self.snap, self.ev = log, snap, evidence
        self.unknowns = sorted(evidence.unknown)
        if len(self.unknowns) > 22:
            raise SystemExit("%d unresolved transactions: enumeration would "
                             "not be practical" % len(self.unknowns))
        self.checks = {r.name: [check_fn(c, r.colnames) for c in r.checks]
                       for r in relations}
        self.funnel = {}

    def candidates(self):
        ks = self.unknowns
        for bits in itertools.product(("committed", "aborted"), repeat=len(ks)):
            yield dict(zip(ks, bits))

    def hint_ok(self, asg):
        for x, want in self.ev.hints.items():
            if x in asg and asg[x] != want:
                return False
        return True

    def visible_state(self, asg):
        out = {}
        fn = outcome_fn(self.ev, asg)
        for rel in self.relations:
            vis = [t for t in rel.tuples
                   if tuple_visible(t, self.snap, self.log, fn)]
            out[rel.name] = vis
        return out

    def constraints_ok(self, state):
        keysets = {}
        for rel in self.relations:
            keys = {}
            for t in state[rel.name]:
                k = rel.key_of(t)
                if any(v is None for v in k):
                    return False
                if k in keys:
                    return False              # PK / UNIQUE violated
                keys[k] = t
                for i, a in enumerate(rel.attrs):
                    if not a.nullable and t.values[i] is None:
                        return False          # NOT NULL violated
                for c in self.checks[rel.name]:
                    if not c(t.values):
                        return False          # CHECK violated
            keysets[rel.name] = keys
        for rel in self.relations:
            for fk in rel.fks:
                src_idx = [rel.colnames.index(c) for c in fk["columns"]]
                parent = keysets[fk["references"]]
                for t in state[rel.name]:
                    ref = tuple(t.values[i] for i in src_idx)
                    if any(v is None for v in ref):
                        continue
                    if ref not in parent:
                        return False          # FK violated
        return True

    def solve(self):
        total = 2 ** len(self.unknowns)
        after_hints = after_pk = after_all = 0
        survivors = []
        outputs = set()
        for asg in self.candidates():
            if not self.hint_ok(asg):
                continue
            after_hints += 1
            state = self.visible_state(asg)
            # staged funnel accounting: PK/UNIQUE+NOT NULL+CHECK first
            keys_ok = True
            for rel in self.relations:
                seen = set()
                for t in state[rel.name]:
                    k = rel.key_of(t)
                    if k in seen or any(v is None for v in k):
                        keys_ok = False
                        break
                    seen.add(k)
                if not keys_ok:
                    break
            if not keys_ok:
                continue
            after_pk += 1
            if not self.constraints_ok(state):
                continue
            after_all += 1
            survivors.append(asg)
            out_rel = next(r for r in self.relations)
            outputs.add(tuple(sorted(
                tuple(t.values) for t in state[self.relations[0].name])))
            if len(survivors) > 8:
                break
        self.funnel = {
            "unresolved_xids": len(self.unknowns),
            "total_assignments": total,
            "after_hint_filter": after_hints,
            "after_key_uniqueness": after_pk,
            "after_all_constraints": after_all,
            "distinct_outputs": len(outputs),
        }
        return survivors


# ---------------------------------------------------------------- driver
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
    ap.add_argument("--dir", default="/app")
    ap.add_argument("--schema", default=None)
    ap.add_argument("--out", default="/app/recovered.csv")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args(argv)

    base = Path(args.dir)
    schema_path = Path(args.schema) if args.schema else base / "table_schema.json"
    js = json.loads(schema_path.read_text(encoding="utf-8"))
    if int(js.get("block_size", BLCKSZ)) != BLCKSZ:
        raise SystemExit("only 8192-byte blocks are implemented")

    relations = [Relation(meta, base) for meta in js["relations"]]
    out_name = js["output_relation"]
    relations.sort(key=lambda r: 0 if r.name == out_name else 1)

    log = TransactionLog(base / "pg_xact", base / "pg_subtrans")
    snap = Snapshot(int(js["snapshot_xmin"]), int(js["snapshot_xmax"]),
                    [int(x) for x in js["snapshot_xip"]])

    evidence = Evidence(relations, log, snap)
    engine = Engine(relations, log, snap, evidence)
    survivors = engine.solve()

    if len(survivors) != 1:
        raise SystemExit(
            "the evidence admits %d consistent transaction-outcome "
            "assignment(s); refusing to guess (funnel: %r)"
            % (len(survivors), engine.funnel))

    final = survivors[0]
    fn = outcome_fn(evidence, final)
    out_rel = relations[0]
    visible = {}
    for t in out_rel.tuples:
        if tuple_visible(t, snap, log, fn):
            k = out_rel.key_of(t)
            if k in visible:
                raise SystemExit("two visible versions of key %r" % (k,))
            visible[k] = t

    out = Path(args.out)
    with out.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(out_rel.colnames)
        for k in sorted(visible):
            w.writerow([format_value(v) for v in visible[k].values])

    if args.report:
        direct = sorted(x for x in engine.unknowns if x in evidence.hints)
        indirect = sorted(x for x in engine.unknowns
                          if x not in evidence.hints)
        print(json.dumps({
            "relations": {r.name: {"heap_bytes": r.heap_bytes,
                                   "tuples": len(r.tuples),
                                   "census": r.census} for r in relations},
            "snapshot": {"xmin": snap.xmin, "xmax": snap.xmax,
                         "xip": sorted(snap.xip)},
            "recorded_outcomes": len(evidence.recorded),
            "unresolved_xids": engine.unknowns,
            "irrelevant_unrecorded_xids": sorted(evidence.irrelevant),
            "direct_hint_xids": direct,
            "indirect_xids": indirect,
            "hint_facts": {str(x): {"outcome": o,
                                    "sources": evidence.hint_sources.get(x, [])}
                           for x, o in sorted(evidence.hints.items())},
            "funnel": engine.funnel,
            "final_assignment": {str(x): final[x] for x in sorted(final)},
            "visible_rows": len(visible),
            "output": str(out),
        }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
