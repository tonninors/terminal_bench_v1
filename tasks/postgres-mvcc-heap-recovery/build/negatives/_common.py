"""Shared plumbing for the v5 negative solvers.

Each negative reuses the oracle's parsing and evidence machinery (reading a
PostgreSQL page or an SLRU segment is not the mistake being modelled) and
replaces exactly the inference step named in its file, so the failure the
verifier detects is the reasoning error, not a decoding bug.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

TASK = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(TASK / "solution"))

import golden_recover as G          # noqa: E402

ART = TASK / "artifacts"


def standard_args(description: str):
    ap = argparse.ArgumentParser(description=description)
    ap.add_argument("--dir", default=str(ART))
    ap.add_argument("--out", required=True)
    return ap


def load(args):
    base = Path(args.dir)
    js = json.loads((base / "table_schema.json").read_text(encoding="utf-8"))
    relations = [G.Relation(meta, base) for meta in js["relations"]]
    out_name = js["output_relation"]
    relations.sort(key=lambda r: 0 if r.name == out_name else 1)
    log = G.TransactionLog(base / "pg_xact", base / "pg_subtrans")
    snap = G.Snapshot(int(js["snapshot_xmin"]), int(js["snapshot_xmax"]),
                      [int(x) for x in js["snapshot_xip"]])
    evidence = G.Evidence(relations, log, snap)
    return js, relations, log, snap, evidence


def emit(args, out_rel, visible_tuples):
    rows = {}
    for t in visible_tuples:
        k = out_rel.key_of(t)
        rows.setdefault(k, []).append(t)
    with open(args.out, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(out_rel.colnames)
        for k in sorted(rows, key=lambda kk: tuple(str(v) for v in kk)):
            for t in rows[k]:
                w.writerow([G.format_value(v) for v in t.values])
    n = sum(len(v) for v in rows.values())
    print("wrote %d row(s) to %s" % (n, args.out))


def visible_under(relations, snap, log, evidence, assignment, default=None):
    """Visible tuples of the OUTPUT relation for a fixed outcome mapping.

    `assignment` maps unresolved xids to committed/aborted; unresolved xids
    absent from it fall back to `default` (None = in-progress)."""
    rec = evidence.recorded

    def outcome(xid):
        st = rec.get(xid)
        if st is not None:
            return st
        if xid in assignment:
            return assignment[xid]
        if xid in evidence.unknown:
            return default
        return None

    out_rel = relations[0]
    return [t for t in out_rel.tuples
            if G.tuple_visible(t, snap, log, outcome)]
