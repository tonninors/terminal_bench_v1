#!/usr/bin/env python3
"""Run every wrong strategy and measure how far off it lands.

Output feeds VERIFIER_SPEC.md and DIFFICULTY_EXPLANATION.md.  The reference is
the oracle's answer, which build/run_all_validation.sh separately proves
identical to the state PostgreSQL itself reported for the target snapshot.

    python3 build/measure_negatives.py [--json build/internal/negative_scores.json]
"""
from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import tempfile
from pathlib import Path

TASK = Path(__file__).resolve().parent.parent
NEG = TASK / "build" / "negatives"
ART = TASK / "artifacts"
ORACLE = TASK / "solution" / "golden_recover.py"

STD = ["--dir", str(ART)]

STRATEGIES = [
    ("1. zero-fill: missing clog = aborted", "neg01_zero_fill_aborted.py"),
    ("2. missing clog = still in progress", "neg02_missing_in_progress.py"),
    ("3. discard tuples with unresolved xmin", "neg03_discard_unresolved.py"),
    ("4. hint bits only, unhinted -> aborted", "neg04_hints_only.py"),
    ("5. fate resolved per tuple, not per xid", "neg05_per_tuple.py"),
    ("6. each relation recovered on its own", "neg06_per_relation.py"),
    ("7. foreign keys ignored", "neg07_ignore_fk.py"),
    ("8. PK/UNIQUE constraints ignored", "neg08_ignore_pk.py"),
    ("9. first locally valid candidate", "neg09_first_candidate.py"),
    ("10. greedy one-hop hint propagation", "neg10_greedy_hints.py"),
    ("11. newest xmin per key (V1 shortcut)", "neg11_newest_xmin.py"),
    ("12. V3/V4 pipeline, no inference layer", "neg12_v4_pipeline.py"),
]


def read_rows(path: Path):
    with path.open(encoding="utf-8", newline="") as fh:
        rows = [r for r in csv.reader(fh) if r]
    return rows[0], rows[1:]


def score(correct_rows, cand_rows):
    want = {r[0]: tuple(r) for r in correct_rows}
    keys = [r[0] for r in cand_rows]
    dup = len(keys) - len(set(keys))
    got_keys = set(keys)
    missing = sorted(set(want) - got_keys, key=int)
    extra = sorted(got_keys - set(want), key=int)
    wrong = sorted({r[0] for r in cand_rows
                    if r[0] in want and tuple(r) != want[r[0]]}, key=int)
    seen, repeated = set(), set()
    for k in keys:
        (repeated if k in seen else seen).add(k)
    bad_keys = set(missing) | set(extra) | set(wrong) | repeated
    return {
        "rows": len(cand_rows),
        "duplicate_key_records": dup,
        "missing": len(missing),
        "extra": len(extra),
        "wrong_values": len(wrong),
        "duplicated_keys": len(repeated),
        "keys_wrong_in_total": len(bad_keys),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=str(TASK / "build" / "internal" /
                                          "negative_scores.json"))
    args = ap.parse_args()

    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        good = d / "correct.csv"
        r = subprocess.run([sys.executable, str(ORACLE), *STD,
                            "--out", str(good)],
                           capture_output=True, text=True)
        if r.returncode:
            raise SystemExit("the oracle failed: " + r.stderr)
        _header, correct_rows = read_rows(good)

        results = {}
        print("reference answer: %d rows\n" % len(correct_rows))
        print("%-44s %6s %6s %8s %6s %6s %9s"
              % ("naive strategy", "rows", "dupPK", "missing", "extra", "wrong",
                 "keys off"))
        print("-" * 94)
        for label, script in STRATEGIES:
            out = d / (script + ".csv")
            cmd = [sys.executable, str(NEG / script), *STD, "--out", str(out)]
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode:
                raise SystemExit("%s failed:\n%s\n%s" % (script, r.stdout, r.stderr))
            _h, rows = read_rows(out)
            s = score(correct_rows, rows)
            results[label] = dict(s, script=script)
            print("%-44s %6d %6d %8d %6d %6d %9d"
                  % (label, s["rows"], s["duplicate_key_records"], s["missing"],
                     s["extra"], s["wrong_values"], s["keys_wrong_in_total"]))

    payload = {"reference_rows": len(correct_rows), "strategies": results}
    Path(args.json).write_text(json.dumps(payload, indent=2) + "\n",
                               encoding="utf-8")
    print("\nwrote", Path(args.json).relative_to(TASK).as_posix())
    weakest = min(results.items(), key=lambda kv: kv[1]["keys_wrong_in_total"])
    print("weakest strategy: %s is wrong on %d key(s)"
          % (weakest[0], weakest[1]["keys_wrong_in_total"]))
    if weakest[1]["keys_wrong_in_total"] == 0:
        raise SystemExit("a negative strategy produced the CORRECT answer")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
