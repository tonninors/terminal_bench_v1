#!/usr/bin/env python3
"""Derive tests/expected_state.json from the hidden PostgreSQL reference answer.

The verifier fixture is built from `build/internal/golden.csv`, which was
produced by the PostgreSQL server itself inside the target snapshot's repeatable
read transaction - never from the oracle's output.  That keeps the verifier
independent of the page-level recovery code it grades.
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

TASK = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TASK / "tests"))

GOLDEN = TASK / "build" / "internal" / "golden.csv"
SCHEMA = TASK / "artifacts" / "table_schema.json"
OUT = TASK / "tests" / "expected_state.json"


def main() -> int:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    rel = next(r for r in schema["relations"]
               if r["name"] == schema["output_relation"])
    columns = [c["name"] for c in rel["columns"]]
    coltypes = [c["type"] for c in rel["columns"]]
    nullable = [c["name"] for c in rel["columns"] if c["nullable"]]
    pk = rel["primary_key"]
    pk_pos = [columns.index(c) for c in pk]

    with GOLDEN.open("r", encoding="utf-8", newline="") as fh:
        rows = list(csv.reader(fh))
    header, data = rows[0], [r for r in rows[1:] if r]
    if header != columns:
        raise SystemExit("golden.csv header %r does not match the schema %r"
                         % (header, columns))

    # Import the verifier's own canonicaliser so the fixture is expressed in
    # exactly the representation the verifier compares against.
    import test_outputs as V

    keyed = {}
    for row in data:
        rowid = "/".join(row[i] for i in pk_pos)
        canon = V.canon_row(row, columns, coltypes, rowid)
        key = tuple(canon[i] for i in pk_pos)
        if key in keyed:
            raise SystemExit("golden.csv has a duplicate primary key %r" % (key,))
        keyed[key] = canon

    null_cells = sorted(
        [list(k) + [columns[i]] for k, r in keyed.items()
         for i, v in enumerate(r) if v == "\x00NULL"])

    expected = {
        "table_name": schema["output_relation"],
        "postgres_version": schema["postgres_version"],
        "columns": columns,
        "column_types": coltypes,
        "nullable_columns": nullable,
        "primary_key": pk,
        "row_count": len(keyed),
        "primary_keys": [list(k) for k in sorted(keyed)],
        "null_cells": null_cells,
        "rows": [{"key": list(k), "row": list(keyed[k])} for k in sorted(keyed)],
        "sha256": V.row_digest(keyed),
        "snapshot": {"xmin": schema["snapshot_xmin"],
                     "xmax": schema["snapshot_xmax"],
                     "xip": schema["snapshot_xip"]},
    }
    OUT.write_text(json.dumps(expected, indent=1, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    print("wrote %s: %d rows, %d NULL cells, sha256 %s"
          % (OUT.relative_to(TASK), expected["row_count"], len(null_cells),
             expected["sha256"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
