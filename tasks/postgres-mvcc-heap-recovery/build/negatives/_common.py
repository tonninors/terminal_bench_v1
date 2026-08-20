"""Shared plumbing for the intentionally wrong candidate answers.

Each negative reuses the oracle's *parsing* code (reading a PostgreSQL page is
not the mistake being modelled) and replaces only the visibility decision, so
the failure that the verifier detects is squarely the reasoning error named in
the file, not an unrelated decoding bug.
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

TASK = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(TASK / "solution"))

import golden_recover as G          # noqa: E402

ART = TASK / "artifacts"


def standard_args(description: str):
    ap = argparse.ArgumentParser(description=description)
    ap.add_argument("--heap", default=str(ART / "heap_pages.bin"))
    ap.add_argument("--pg-xact", dest="pg_xact", default=str(ART / "pg_xact"))
    ap.add_argument("--pg-subtrans", dest="pg_subtrans",
                    default=str(ART / "pg_subtrans"))
    ap.add_argument("--pg-multixact", dest="pg_multixact",
                    default=str(ART / "pg_multixact"))
    ap.add_argument("--schema", default=str(ART / "table_schema.json"))
    ap.add_argument("--out", required=True)
    return ap


def load(args):
    js, attrs, snap = G.load_schema(Path(args.schema))
    status = G.load_transaction_log(Path(args.pg_xact),
                                    Path(args.pg_subtrans),
                                    Path(args.pg_multixact))
    heap = Path(args.heap).read_bytes()
    tuples = []
    for blk in range(len(heap) // 8192):
        t, _c, _h = G.parse_page(heap[blk * 8192:(blk + 1) * 8192], blk, attrs)
        tuples.extend(t)
    return js, attrs, snap, status, tuples


def deleting_xid(t, log):
    """The xid that would delete this tuple under CORRECT lock/multixact
    handling, or None when the xmax is absent, invalid, or lock-only.

    Negatives that model a snapshot- or status-level flaw call this first, so
    the flaw they measure stays the one they are named for."""
    if t.infomask & 0x0800 or not t.xmax:        # HEAP_XMAX_INVALID / none
        return None
    if t.infomask & 0x1000:                      # HEAP_XMAX_IS_MULTI
        if t.infomask & 0x0080:                  # lock-only multi
            return None
        return log.multi.updater(t.xmax)         # may be None
    if G.xmax_is_locked_only(t.infomask):
        return None
    return t.xmax


def pk_positions(js, attrs):
    names = [a.name for a in attrs]
    return [names.index(c) for c in js["primary_key"]]


def write_csv(path, attrs, rows, nulls=r"\N", columns=None):
    cols = columns if columns is not None else [a.name for a in attrs]
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(cols)
        for values in rows:
            w.writerow([nulls if v is None else G.format_value(v) for v in values])
    print("wrote %d row(s) to %s" % (len(rows), path))
