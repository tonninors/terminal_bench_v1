#!/usr/bin/env python3
"""Run every wrong strategy and measure how far off it lands.

Output feeds VERIFIER_SPEC.md and V1_VS_V2_DIFFICULTY.md.  The reference is the
oracle's answer, which build/run_all_validation.sh separately proves identical
to the state PostgreSQL itself reported for the target snapshot.

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

STD = ["--heap", str(ART / "heap_pages.bin"),
       "--pg-xact", str(ART / "pg_xact"),
       "--pg-subtrans", str(ART / "pg_subtrans"),
       "--pg-multixact", str(ART / "pg_multixact"),
       "--schema", str(ART / "table_schema.json")]

# label -> (script, needs the correct answer as --src)
STRATEGIES = [
    ("1. greatest xmin per key", "negative_a_max_xmin.py", False),
    ("2. ignore xmax entirely", "negative_b_ignore_xmax.py", False),
    ("3. committed xmax means deleted", "negative_l_committed_xmax_deleted.py", False),
    ("4a. HOT redirects walked as tuples", "negative_d_ignore_hot.py", False),
    ("4b. heap-only versions skipped", "negative_d2_skip_heap_only.py", False),
    ("5. aborted treated as committed", "negative_c_aborted_visible.py", False),
    ("6. in-progress treated as committed", "negative_e_in_progress_committed.py", False),
    ("7a. snapshot_xip ignored", "negative_j_ignore_xip.py", False),
    ("7b. every recent xid is in progress", "negative_j2_recent_is_in_progress.py", False),
    ("8. hint bits used as the commit log", "negative_i_hint_bits_only.py", False),
    ("9. every physical tuple", "negative_f_all_tuples.py", False),
    ("10. newest committed, no header state", "negative_m_newest_committed.py", False),
    ("11. pg_subtrans ignored", "negative_p_ignore_subtrans.py", False),
    ("12. subxact inherits parent status", "negative_q_subxact_inherits_parent.py", False),
    ("13. multi xmax read as a plain xid", "negative_r_multi_as_xid.py", False),
    ("14. every multi is just a lock", "negative_s_multi_always_lock.py", False),
    ("15. any committed member kills", "negative_t_any_member_kills.py", False),
    ("16. highest member assumed updater", "negative_t2_highest_member_updates.py", False),
    ("17. multi updater without pg_subtrans", "negative_u_updater_no_subtrans.py", False),
    ("18. multi updater ignores the snapshot", "negative_v_updater_ignore_snapshot.py", False),
    ("19. reserved member offset 0 mishandled", "negative_w_offset_off_by_one.py", False),
    ("20. member range read until a zero xid", "negative_x_range_until_zero.py", False),
    ("21. XMIN_INVALID tested before FROZEN", "negative_y_invalid_before_frozen.py", False),
    ("22. frozen means visible, xmax skipped", "negative_z_frozen_always_visible.py", False),
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
        r = subprocess.run([sys.executable, str(ORACLE), *STD, "--out", str(good)],
                           capture_output=True, text=True)
        if r.returncode:
            raise SystemExit("the oracle failed: " + r.stderr)
        _header, correct_rows = read_rows(good)

        results = {}
        print("reference answer: %d rows\n" % len(correct_rows))
        print("%-42s %6s %6s %8s %6s %6s %9s"
              % ("naive strategy", "rows", "dupPK", "missing", "extra", "wrong",
                 "keys off"))
        print("-" * 92)
        for label, script, needs_src in STRATEGIES:
            out = d / (script + ".csv")
            cmd = [sys.executable, str(NEG / script)]
            cmd += ["--src", str(good)] if needs_src else STD
            cmd += ["--out", str(out)]
            r = subprocess.run(cmd, capture_output=True, text=True)
            if r.returncode:
                raise SystemExit("%s failed:\n%s\n%s" % (script, r.stdout, r.stderr))
            _h, rows = read_rows(out)
            s = score(correct_rows, rows)
            results[label] = dict(s, script=script)
            print("%-42s %6d %6d %8d %6d %6d %9d"
                  % (label, s["rows"], s["duplicate_key_records"], s["missing"],
                     s["extra"], s["wrong_values"], s["keys_wrong_in_total"]))

    payload = {"reference_rows": len(correct_rows), "strategies": results}
    Path(args.json).write_text(json.dumps(payload, indent=2) + "\n",
                               encoding="utf-8")
    print("\nwrote", Path(args.json).relative_to(TASK).as_posix())
    weakest = min(results.items(), key=lambda kv: kv[1]["keys_wrong_in_total"])
    print("weakest strategy: %s is wrong on %d key(s)"
          % (weakest[0], weakest[1]["keys_wrong_in_total"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
