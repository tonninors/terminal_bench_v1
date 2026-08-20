#!/usr/bin/env python3
"""Alternative correct solution - a second implementation from scratch.

Shares no code with solution/golden_recover.py: its own page and tuple
decoder, its own SLRU readers, its own snapshot logic, and a depth-first
search over the unresolved transactions instead of the oracle's product
enumeration.  It also spells the output differently - CRLF line endings,
TRUE/FALSE booleans, rows in descending key order - to prove the verifier
grades the recovered state, not the bytes or the method.

    alt_independent_parser.py --dir artifacts --out recovered.csv
"""
from __future__ import annotations

import argparse
import csv
import datetime
import json
import re
import struct
import sys
from pathlib import Path

BLOCK = 8192
EPOCH = datetime.date(2000, 1, 1)

# infomask bits, transcribed from htup_details.h
XMAX_KEYSHR = 0x0010
XMAX_EXCL = 0x0040
XMAX_LOCK_ONLY = 0x0080
HASNULL = 0x0001
XMIN_COMMITTED = 0x0100
XMIN_INVALID = 0x0200
XMAX_COMMITTED = 0x0400
XMAX_INVALID = 0x0800
XMAX_IS_MULTI = 0x1000
NATTS_MASK = 0x07FF

FIXED = {"integer": (4, 4), "smallint": (2, 2), "bigint": (8, 8),
         "boolean": (1, 1), "date": (4, 4)}


def align_up(pos, a):
    return -(-pos // a) * a


def base_type(typename):
    return typename.split("(")[0].strip().lower()


# ----------------------------------------------------------------- heap pages
def decode_datum(body, pos, typename):
    """Returns (python value, next position)."""
    bt = base_type(typename)
    if bt in FIXED:
        size, a = FIXED[bt]
        pos = align_up(pos, a)
        raw = body[pos:pos + size]
        if bt == "boolean":
            return raw[0] != 0, pos + size
        n = int.from_bytes(raw, "little", signed=True)
        if bt == "date":
            return (EPOCH + datetime.timedelta(days=n)).isoformat(), pos + size
        return n, pos + size
    # varlena (text / varchar): 1-byte header when the low bit is set
    if body[pos] == 0x00:
        pos = align_up(pos, 4)
    head = body[pos]
    if head == 0x01:
        raise SystemExit("TOAST pointer found; the task promises inline data")
    if head & 0x01:
        total = head >> 1
        return body[pos + 1:pos + total].decode("utf-8"), pos + total
    word = struct.unpack_from("<I", body, pos)[0]
    if word & 0x03:
        raise SystemExit("compressed datum found")
    total = word >> 2
    return body[pos + 4:pos + total].decode("utf-8"), pos + total


def read_heap(path, columns):
    """Yield one dict per LP_NORMAL tuple."""
    data = Path(path).read_bytes()
    if len(data) % BLOCK:
        raise SystemExit(f"{path} is not whole blocks")
    for blk in range(len(data) // BLOCK):
        page = data[blk * BLOCK:(blk + 1) * BLOCK]
        pd_lower, pd_upper = struct.unpack_from("<HH", page, 12)
        if pd_lower == 0:
            continue
        nslots = (pd_lower - 24) // 4
        for slot in range(nslots):
            word = struct.unpack_from("<I", page, 24 + 4 * slot)[0]
            off, flags, length = word & 0x7FFF, (word >> 15) & 3, word >> 17
            if flags != 1:              # only LP_NORMAL carries a tuple
                continue
            body = page[off:off + length]
            xmin, xmax = struct.unpack_from("<II", body, 0)
            infomask2, infomask, hoff = struct.unpack_from("<HHB", body, 18)
            if infomask & XMAX_IS_MULTI:
                raise SystemExit("MultiXact xmax: outside this task's scope")
            natts = infomask2 & NATTS_MASK
            bitmap = body[23:23 + (natts + 7) // 8] \
                if infomask & HASNULL else None
            fields = {}
            pos = hoff
            for i, col in enumerate(columns):
                if i >= natts or (bitmap is not None
                                  and not bitmap[i // 8] >> (i % 8) & 1):
                    fields[col["name"]] = None
                    continue
                fields[col["name"]], pos = decode_datum(body, pos,
                                                        col["type"])
            yield {"block": blk, "slot": slot + 1, "xmin": xmin, "xmax": xmax,
                   "mask": infomask, "fields": fields}


def lock_only(mask):
    if mask & XMAX_LOCK_ONLY:
        return True
    return (mask & (XMAX_IS_MULTI | XMAX_KEYSHR | XMAX_EXCL)) == XMAX_EXCL


# ------------------------------------------------------------------ SLRU data
def load_slru(directory):
    segs = {}
    for f in sorted(Path(directory).iterdir()):
        if f.is_file() and re.fullmatch(r"[0-9A-Fa-f]{4,}", f.name):
            segs[int(f.name, 16)] = f.read_bytes()
    if not segs:
        raise SystemExit(f"{directory} holds no SLRU segments")
    return segs


class Clog:
    def __init__(self, xact_dir, subtrans_dir):
        self.xact = load_slru(xact_dir)
        self.sub = load_slru(subtrans_dir)

    def bits(self, xid):
        page, at = divmod(xid, BLOCK * 4)
        seg, page = divmod(page, 32)
        blob = self.xact.get(seg)
        if blob is None or page * BLOCK + at // 4 >= len(blob):
            return 0
        return blob[page * BLOCK + at // 4] >> (at % 4 * 2) & 3

    def parent_of(self, xid):
        page, at = divmod(xid, BLOCK // 4)
        seg, page = divmod(page, 32)
        blob = self.sub.get(seg)
        if blob is None:
            return 0
        return struct.unpack_from("<I", blob, page * BLOCK + at * 4)[0]

    def top_of(self, xid):
        while True:
            p = self.parent_of(xid)
            if not p or p >= xid:
                return xid
            xid = p

    def recorded(self, xid):
        """'c'/'a' when the surviving file decides it, else None."""
        while True:
            b = self.bits(xid)
            if b == 1:
                return "c"
            if b == 2:
                return "a"
            if b == 3:                  # subcommitted: the parent decides
                p = self.parent_of(xid)
                if not p:
                    return None
                xid = p
                continue
            return None


class Snap:
    def __init__(self, lo, hi, running, clog):
        self.lo, self.hi, self.running, self.clog = lo, hi, set(running), clog

    def open_at_snapshot(self, xid):
        if xid >= self.hi:
            return True
        if xid < self.lo:
            return False
        if xid in self.running:
            return True
        top = self.clog.top_of(xid)
        if top == xid:
            return False
        return top >= self.hi or (top >= self.lo and top in self.running)


# ------------------------------------------------------------------ inference
def gather(schema, base):
    tables = {}
    for meta in schema["relations"]:
        tables[meta["name"]] = {
            "meta": meta,
            "tuples": list(read_heap(base / meta["file"], meta["columns"])),
        }
    return tables


def evidence(tables, clog, snap):
    known, open_now, unknown, facts = {}, set(), set(), {}
    xids = set()
    for tab in tables.values():
        for t in tab["tuples"]:
            xids.add(t["xmin"])
            if t["xmax"]:
                xids.add(t["xmax"])
    xids.discard(0)
    for x in xids:
        st = clog.recorded(x)
        if st:
            known[x] = st
        elif snap.open_at_snapshot(x):
            open_now.add(x)
        else:
            unknown.add(x)
    for tab in tables.values():
        for t in tab["tuples"]:
            m = t["mask"]
            if (m & (XMIN_COMMITTED | XMIN_INVALID)) not in \
                    (0, XMIN_COMMITTED | XMIN_INVALID):
                fact = "c" if m & XMIN_COMMITTED else "a"
                if t["xmin"] in unknown:
                    prev = facts.setdefault(t["xmin"], fact)
                    if prev != fact:
                        raise SystemExit("contradictory xmin hints")
            if t["xmax"] and not lock_only(m):
                if m & XMAX_COMMITTED and t["xmax"] in unknown:
                    facts.setdefault(t["xmax"], "c")
                elif m & XMAX_INVALID and t["xmax"] in unknown:
                    facts.setdefault(t["xmax"], "a")
    return known, open_now, unknown, facts


def alive(t, verdict, snap):
    if verdict(t["xmin"]) != "c" or snap.open_at_snapshot(t["xmin"]):
        return False
    m = t["mask"]
    if m & XMAX_INVALID or not t["xmax"] or lock_only(m):
        return True
    if verdict(t["xmax"]) != "c":
        return True
    return snap.open_at_snapshot(t["xmax"])


CHECK = re.compile(r"^\s*(\w+)\s*(<>|!=|>=|<=|>|<|=)\s*(-?\d+)\s*$")
OPS = {"<>": lambda a, b: a != b, "!=": lambda a, b: a != b,
       ">": lambda a, b: a > b, "<": lambda a, b: a < b,
       ">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b,
       "=": lambda a, b: a == b}


def state_is_legal(tables, verdict, snap):
    live = {name: [t for t in tab["tuples"] if alive(t, verdict, snap)]
            for name, tab in tables.items()}
    keysets = {}
    for name, rows in live.items():
        meta = tables[name]["meta"]
        pk = meta["primary_key"]
        keys = set()
        for t in rows:
            f = t["fields"]
            for col in meta["columns"]:
                if not col["nullable"] and f[col["name"]] is None:
                    return False, None
            for expr in meta.get("checks") or []:
                m = CHECK.match(expr)
                v = f[m.group(1)]
                if v is not None and not OPS[m.group(2)](v, int(m.group(3))):
                    return False, None
            k = tuple(f[c] for c in pk)
            if k in keys:
                return False, None
            keys.add(k)
        keysets[name] = keys
    for name, rows in live.items():
        for fk in tables[name]["meta"].get("foreign_keys") or []:
            parents = keysets[fk["references"]]
            for t in rows:
                ref = tuple(t["fields"][c] for c in fk["columns"])
                if None not in ref and ref not in parents:
                    return False, None
    return True, live


def search(tables, clog, snap):
    known, open_now, unknown, facts = evidence(tables, clog, snap)
    free = sorted(unknown - set(facts))

    def verdict_for(extra):
        def verdict(xid):
            if xid in known:
                return known[xid]
            if xid in facts:
                return facts[xid]
            if xid in extra:
                return extra[xid]
            return None                  # open at the snapshot, or bogus
        return verdict

    solutions = []

    def dfs(i, chosen):
        if i == len(free):
            ok, live = state_is_legal(tables, verdict_for(chosen), snap)
            if ok:
                solutions.append((dict(chosen), live))
            return
        for pick in ("c", "a"):
            chosen[free[i]] = pick
            dfs(i + 1, chosen)
        del chosen[free[i]]

    dfs(0, {})
    if len(solutions) != 1:
        raise SystemExit(f"{len(solutions)} consistent global outcomes; "
                         "refusing to guess")
    return solutions[0][1]


# ---------------------------------------------------------------------- output
def render(value, typename):
    if value is None:
        return "\\N"
    if base_type(typename) == "boolean":
        return "TRUE" if value else "FALSE"
    return str(value)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=str(Path(__file__).resolve()
                                         .parents[2] / "artifacts"))
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    base = Path(a.dir)

    schema = json.loads((base / "table_schema.json").read_text("utf-8"))
    clog = Clog(base / "pg_xact", base / "pg_subtrans")
    snap = Snap(int(schema["snapshot_xmin"]), int(schema["snapshot_xmax"]),
                [int(x) for x in schema["snapshot_xip"]], clog)
    tables = gather(schema, base)
    live = search(tables, clog, snap)

    meta = next(m for m in schema["relations"]
                if m["name"] == schema["output_relation"])
    pk = meta["primary_key"]
    rows = sorted(live[meta["name"]],
                  key=lambda t: tuple(t["fields"][c] for c in pk),
                  reverse=True)
    with open(a.out, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, lineterminator="\r\n")
        w.writerow([c["name"] for c in meta["columns"]])
        for t in rows:
            w.writerow([render(t["fields"][c["name"]], c["type"])
                        for c in meta["columns"]])
    print(f"recovered {len(rows)} row(s) -> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
