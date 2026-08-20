#!/usr/bin/env python3
"""Final audit: re-check every claim the task package makes.

Run from the task root:  python3 build/final_audit.py

Covers the release checklist - genuine PostgreSQL 16 pages, 8192-byte blocks,
exactly three solver inputs, a fully specified snapshot, a unique answer, the
aborted / in-progress / committed-inside-xip cases, ZIP contents and leak
checks, oracle and verifier scope, and a two-way match between every
requirement in FINAL_PROMPT.txt and the check that enforces it.
"""
import json, csv, zipfile, struct, pathlib, io, sys
fails = []
def chk(b, m):
    print(("  [ OK ] " if b else "  [FAIL] ") + m)
    if not b: fails.append(m)

print("=== FINAL AUDIT ===\n")
heap = pathlib.Path("artifacts/heap_pages.bin").read_bytes()
schema = json.loads(pathlib.Path("artifacts/table_schema.json").read_text(encoding="utf-8"))
slru = {a: sorted(p.name for p in pathlib.Path("artifacts", a).iterdir()
                  if p.is_file())
        for a in ("pg_xact", "pg_subtrans")}
exp = json.loads(pathlib.Path("tests/expected_state.json").read_text(encoding="utf-8"))
rep = json.loads(pathlib.Path("build/internal/generation_report.json").read_text(encoding="utf-8"))
prompt_raw = pathlib.Path("FINAL_PROMPT.txt").read_text(encoding="utf-8")
prompt = " ".join(prompt_raw.split())   # line wrapping is not meaningful
taskyaml = pathlib.Path("task.yaml").read_text(encoding="utf-8")
NULL_REQ = "SQL NULL as " + chr(92) + "N"

chk("PostgreSQL 16." in schema["postgres_version_full"], "genuine PostgreSQL 16 heap pages (%s)" % schema["postgres_version"])
lv = [struct.unpack_from("<H", heap, b*8192+18)[0] for b in range(len(heap)//8192)]
chk(all(v & 0xFF00 == 8192 and v & 0xFF == 4 for v in lv), "every block is 8192 bytes, page layout version 4")
chk(len(heap) % 8192 == 0 and schema["block_size"] == 8192, "len(heap_pages.bin) %% 8192 == 0 (%d bytes, %d blocks)" % (len(heap), len(heap)//8192))
chk(sorted(p.name for p in pathlib.Path("artifacts").iterdir()) ==
    ["heap_pages.bin", "pg_subtrans", "pg_xact", "table_schema.json"],
    "solver receives exactly heap_pages.bin, table_schema.json, pg_xact/ and pg_subtrans/")
chk(not pathlib.Path("artifacts/tx_status.csv").exists(),
    "tx_status.csv is no longer solver-visible")
for _a in ("pg_xact", "pg_subtrans"):
    chk(bool(slru[_a]), "%s/ ships %d segment(s): %s" % (_a, len(slru[_a]), slru[_a]))
    chk(all(len(n) == 4 and all(c in "0123456789ABCDEFabcdef" for c in n)
            for n in slru[_a]),
        "%s/ uses real four-hex-digit SLRU segment names" % _a)
    chk(all(pathlib.Path("artifacts", _a, n).stat().st_size % 8192 == 0
            for n in slru[_a]),
        "%s/ segments are whole 8192-byte SLRU pages" % _a)
chk(all(k in schema for k in ("snapshot_xmin","snapshot_xmax","snapshot_xip")) and bool(schema["snapshot_xip"]), "target snapshot fully specified: %d:%d:%s" % (schema["snapshot_xmin"], schema["snapshot_xmax"], schema["snapshot_xip"]))
chk(len(exp["primary_keys"]) == len(set(map(tuple, exp["primary_keys"]))) == exp["row_count"], "correct result is unique: %d distinct primary keys" % exp["row_count"])

states = set(rep["transaction_state_counts"])
chk(states == {"committed", "aborted", "in progress"},
    "aborted and in-progress transactions matter: %s" % sorted(states))
chk(len(rep["committed_but_listed_in_xip"]) >= 3,
    "committed transactions inside snapshot_xip: %s" % rep["committed_but_listed_in_xip"])
chk(bool(rep["committed_after_snapshot"]),
    "committed transactions above snapshot_xmax: %s" % rep["committed_after_snapshot"])
chk(rep["subtransaction_parent_entries"] >= 6,
    "%d subtransaction(s) recorded in pg_subtrans" % rep["subtransaction_parent_entries"])
chk(rep["tuple_stamps_requiring_pg_subtrans"] >= 12,
    "%d tuple stamp(s) require pg_subtrans to be resolved"
    % rep["tuple_stamps_requiring_pg_subtrans"])
chk(rep["deepest_subtransaction_chain"] >= 2,
    "deepest subtransaction chain is %d hop(s)" % rep["deepest_subtransaction_chain"])
chk(bool(rep["visible_subtransactions"]),
    "some subtransactions are visible: %s" % rep["visible_subtransactions"])
chk(bool(rep["aborted_children_of_committed_parents"]),
    "aborted children under committed parents: %s"
    % rep["aborted_children_of_committed_parents"])

z = zipfile.ZipFile("dist/postgres_mvcc_heap_inputs_v3.zip")
names = sorted(z.namelist())
want = sorted(["heap_pages.bin", "table_schema.json"] +
              ["pg_xact/" + n for n in slru["pg_xact"]] +
              ["pg_subtrans/" + n for n in slru["pg_subtrans"]])
chk(names == want, "ZIP contents exactly: %s" % names)
chk(not any(i.is_dir() for i in z.infolist()), "no directory entries in the ZIP")
chk(all(n.count("/") == 0 or n.split("/")[0] in ("pg_xact", "pg_subtrans")
        for n in names),
    "the only nested paths are the real SLRU directory names")
blob = b"".join(z.read(n) for n in names)
leaks = [f for f in ["build/internal/golden.csv","tests/expected_state.json","solution/golden_recover.py","tests/test_outputs.py","build/pg_fixture.py"] if pathlib.Path(f).read_bytes() in blob]
chk(not leaks, "ZIP leaks no oracle / golden / verifier / generator content")
chk(exp["sha256"].encode() not in blob, "expected digest is not inside the ZIP")

src = pathlib.Path("solution/golden_recover.py").read_text(encoding="utf-8")
bad = [t for t in ["golden.csv", "expected_state", "internal/", "psycopg",
                   "subprocess", "socket", "urllib", "requests",
                   "tx_status"] if t in src]
chk(not bad, "oracle references only the solver-visible inputs")
chk(all(f in src for f in ("--heap", "--pg-xact", "--pg-subtrans", "--schema")),
    "oracle takes exactly the solver-visible inputs as arguments")

vsrc = pathlib.Path("tests/test_outputs.py").read_text(encoding="utf-8")
bad = [t for t in ["heap_pages", "pg_xact", "pg_subtrans", "table_schema",
                   "golden.csv", "internal/", "subprocess", "os.system",
                   "socket"] if t in vsrc]
chk(not bad, "verifier inspects only /app/recovered.csv (+ its fixture)")
chk('"/app/recovered.csv"' in vsrc, "verifier default target is /app/recovered.csv")

reqs = {
  "UTF-8":                     ("UTF-8" in prompt, "test_output_exists_and_is_utf8" in vsrc),
  "header in schema order":    ("header with the schema columns in" in prompt, "test_header_matches_schema_columns_in_order" in vsrc),
  "one row per visible PK":    ("exactly one row for each primary key visible" in prompt, "test_no_duplicate_primary_keys" in vsrc and "test_no_missing_primary_key" in vsrc),
  "no invisible versions":     ("must not appear" in prompt, "test_no_unexpected_primary_key" in vsrc),
  "NULL written as backslash-N": (NULL_REQ in prompt, "test_null_representation" in vsrc),
  "row order free":            ("Row order is not significant" in prompt, "test_row_order_is_not_significant" in vsrc),
  "RFC 4180 CSV":              ("RFC 4180 CSV" in prompt, "csv.reader" in vsrc),
  "LF or CRLF":                ("LF or CRLF" in prompt, 'newline=""' in vsrc),
  "int in decimal":            ("integer and smallint in decimal" in prompt, "INT_RE" in vsrc),
  "bool t/f/true/false":       ("boolean as t or f" in prompt, "TRUE_TOKENS" in vsrc),
  "date YYYY-MM-DD":           ("YYYY-MM-DD" in prompt, "DATE_RE" in vsrc),
  "empty field is not NULL":   ("An empty field is the empty string, not NULL" in prompt, "empty fields found in nullable columns" in vsrc),
  "raw commit log named":      ("/app/pg_xact/" in prompt, True),
  "subtransaction map named":  ("/app/pg_subtrans/" in prompt, True),
  "no decoded state file":     ("tx_status" not in prompt, True),
  "only recovered.csv graded": ("Only /app/recovered.csv is evaluated" in prompt, True),
}
print()
for k,(inp,inv) in reqs.items():
    chk(inp and inv, "prompt requirement enforced: %s%s" % (k, "" if inp else "  [NOT IN PROMPT]"))

body = taskyaml.split("instruction: |\n",1)[1]
dedented = "\n".join(l[2:] if l.startswith("  ") else l for l in body.split("\n")).strip()
chk(dedented == prompt_raw.strip(), "task.yaml instruction matches FINAL_PROMPT.txt verbatim")

required = ["VERSION_HISTORY.md", "FINAL_PROMPT.txt","FILE_DESCRIPTION.txt","DIFFICULTY_EXPLANATION.md","GOLDEN_SOLUTION.md",
            "VERIFIER_SPEC.md","MANIFEST.md","INTERNAL_NOTES.md","task.yaml","Dockerfile","solution.sh",
            "run-tests.sh","docker-compose.yaml",".gitignore"]
missing = [f for f in required if not pathlib.Path(f).exists()]
chk(not missing, "all required files present" + (" (missing %s)" % missing if missing else ""))
dirs = ["build","solution","tests","artifacts","dist","build/negatives","build/internal"]
chk(all(pathlib.Path(d).is_dir() for d in dirs), "all required directories present")
chk("All solver-facing data and database contents are synthetic and self-created for" in pathlib.Path("DIFFICULTY_EXPLANATION.md").read_text(encoding="utf-8"), "difficulty doc carries the required synthetic-data statement")

# ---- v2: the solver-facing contract is unchanged ------------------------
print()
dockerfile = pathlib.Path("Dockerfile").read_text(encoding="utf-8")
chk("/app/evidence" not in dockerfile,
    "the image no longer creates duplicate inputs under /app/evidence")
for f in ("heap_pages.bin", "table_schema.json"):
    chk("/app/" + f in dockerfile, "the image still provides /app/" + f)
for d in ("pg_xact", "pg_subtrans"):
    chk("/app/%s/" % d in dockerfile, "the image provides /app/%s/" % d)
chk("tx_status" not in dockerfile, "the image no longer copies tx_status.csv")
chk("/app/recovered.csv" in prompt_raw,
    "/app/recovered.csv is still the sole requested output")
chk(not pathlib.Path("dist/postgres_mvcc_heap_inputs.zip").exists()
    and not pathlib.Path("dist/postgres_mvcc_heap_inputs_v2.zip").exists(),
    "the stale v1/v2 bundles have been removed from dist/")
chk(rep["lock_only_tuples_requiring_infomask"] >= 15,
    "lock-only xmax needs the infomask on %d tuple(s)"
    % rep["lock_only_tuples_requiring_infomask"])
chk(len(rep["committed_but_listed_in_xip"]) >= 3,
    "committed transactions listed in snapshot_xip: %s"
    % rep["committed_but_listed_in_xip"])
chk(len(rep["committed_inside_xid_range_but_not_in_xip"]) >= 3,
    "committed transactions inside the xid range but absent from xip: %s"
    % rep["committed_inside_xid_range_but_not_in_xip"])
scores = json.loads(pathlib.Path(
    "build/internal/negative_scores.json").read_text(encoding="utf-8"))
worst = min(v["keys_wrong_in_total"] for v in scores["strategies"].values())
chk(worst >= 10,
    "every measured naive strategy is wrong on >= 10 keys (weakest: %d)" % worst)

print()
print("=" * 46)
print("FINAL AUDIT PASSED" if not fails else "AUDIT FAILURES: %s" % fails)
print("=" * 46)
sys.exit(1 if fails else 0)
