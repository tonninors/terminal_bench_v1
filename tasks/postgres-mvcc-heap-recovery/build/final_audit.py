#!/usr/bin/env python3
"""Final audit: re-check every claim the task package makes (fixture v5).

Run from the task root:  python3 build/final_audit.py

Covers the release checklist - genuine PostgreSQL 16 pages for all three
relations, exactly six solver inputs, a fully specified snapshot, the
authentically stale commit-log tail, the machine-proven unique assignment and
unique output, the designed direct/indirect evidence split, ZIP contents and
leak checks, oracle and verifier scope, and a two-way match between every
requirement in FINAL_PROMPT.txt and the check that enforces it.
"""
import json, zipfile, struct, pathlib, sys
fails = []
def chk(b, m):
    print(("  [ OK ] " if b else "  [FAIL] ") + m)
    if not b: fails.append(m)

print("=== FINAL AUDIT (v5) ===\n")
schema = json.loads(pathlib.Path("artifacts/table_schema.json").read_text(encoding="utf-8"))
slru = {a: sorted(p.name for p in pathlib.Path("artifacts", a).iterdir()
                  if p.is_file())
        for a in ("pg_xact", "pg_subtrans")}
exp = json.loads(pathlib.Path("tests/expected_state.json").read_text(encoding="utf-8"))
rep = json.loads(pathlib.Path("build/internal/generation_report.json").read_text(encoding="utf-8"))
inf = json.loads(pathlib.Path("build/internal/inference_report.json").read_text(encoding="utf-8"))
obs = json.loads(pathlib.Path("build/internal/realism_observations.json").read_text(encoding="utf-8"))
prompt_raw = pathlib.Path("FINAL_PROMPT.txt").read_text(encoding="utf-8")
prompt = " ".join(prompt_raw.split())   # line wrapping is not meaningful
taskyaml = pathlib.Path("task.yaml").read_text(encoding="utf-8")
NULL_REQ = "SQL NULL as " + chr(92) + "N"

HEAPS = ["heap_pages.bin", "ledger_entries_heap.bin", "account_tags_heap.bin"]

chk("PostgreSQL 16." in schema["postgres_version_full"],
    "genuine PostgreSQL 16 heap pages (%s)" % schema["postgres_version"])
for name in HEAPS:
    heap = pathlib.Path("artifacts", name).read_bytes()
    okpages = len(heap) % 8192 == 0
    for b in range(len(heap)//8192):
        lo, up, sp, pv = struct.unpack_from("<HHHH", heap, b*8192+12)
        if lo == 0 and up == 0:
            continue
        okpages &= pv & 0xFF00 == 8192 and pv & 0xFF == 4 \
            and 24 <= lo <= up <= sp <= 8192
    chk(okpages, "%s: %d block(s), 8192 bytes, page layout version 4"
        % (name, len(heap)//8192))
chk(sorted(p.name for p in pathlib.Path("artifacts").iterdir()) ==
    sorted(HEAPS + ["table_schema.json", "pg_xact", "pg_subtrans"]),
    "solver receives exactly the three heaps, table_schema.json, pg_xact/ "
    "and pg_subtrans/")
chk(not pathlib.Path("artifacts/tx_status.csv").exists(),
    "no decoded transaction table is solver-visible")
chk(not pathlib.Path("artifacts/pg_multixact").exists(),
    "pg_multixact/ was retired with fixture v5")
for _a in ("pg_xact", "pg_subtrans"):
    chk(bool(slru[_a]), "%s/ ships %d segment(s): %s" % (_a, len(slru[_a]), slru[_a]))
    chk(all(len(n) == 4 and all(c in "0123456789ABCDEFabcdef" for c in n)
            for n in slru[_a]),
        "%s/ uses real four-hex-digit SLRU segment names" % _a)
    chk(all(pathlib.Path("artifacts", _a, n).stat().st_size % 8192 == 0
            for n in slru[_a]),
        "%s/ segments are whole 8192-byte SLRU pages" % _a)
chk(all(k in schema for k in ("snapshot_xmin", "snapshot_xmax", "snapshot_xip"))
    and bool(schema["snapshot_xip"]),
    "target snapshot fully specified: %d:%d:%s"
    % (schema["snapshot_xmin"], schema["snapshot_xmax"], schema["snapshot_xip"]))
rels = {r["name"]: r for r in schema["relations"]}
chk(set(rels) == {"accounts", "ledger_entries", "account_tags"}
    and schema["output_relation"] == "accounts",
    "three relations declared; accounts is the output relation")
chk(all(r.get("primary_key") for r in rels.values())
    and all(rels[c].get("foreign_keys") for c in ("ledger_entries",
                                                  "account_tags"))
    and rels["accounts"].get("checks") and rels["ledger_entries"].get("checks"),
    "PK on every relation, FKs on both children, CHECKs declared")
chk("incident" in schema and "does not record an outcome" in schema["incident"],
    "the schema states the incident without naming any affected transaction")
chk(len(exp["primary_keys"]) == len(set(map(tuple, exp["primary_keys"])))
    == exp["row_count"],
    "correct result is unique: %d distinct primary keys" % exp["row_count"])

# ---- the stale clog tail and the machine-proven inference -----------------
print()
chk(rep["fixture_version"] == 5, "generation report is fixture v5")
funnel = inf["funnel"]
chk(funnel["after_all_constraints"] == 1 and funnel["distinct_outputs"] == 1,
    "exhaustive enumeration: exactly one assignment, one output (%s)"
    % json.dumps(funnel))
chk(funnel["after_hint_filter"] > 1,
    "hint bits alone leave %d candidates - the constraint system is "
    "load-bearing" % funnel["after_hint_filter"])
chk(funnel["after_key_uniqueness"] > 1,
    "hints+keys still leave %d candidates - the foreign keys are load-bearing"
    % funnel["after_key_uniqueness"])
n_unres = len(inf["unresolved_xids"])
n_ind = len(inf["indirect_xids"])
chk(12 <= n_unres <= 22, "%d unresolved transaction(s), inside [12, 22]" % n_unres)
chk(n_ind >= max(4, int(0.30 * n_unres)),
    "%d of %d unresolved xid(s) have no direct hint anywhere (indirect)"
    % (n_ind, n_unres))
designed = {int(x): d["outcome"] for x, d in rep["designed_truth"].items()}
final = {int(x): o for x, o in inf["final_assignment"].items()}
chk(all(designed.get(x) == o for x, o in final.items()),
    "the unique inferred assignment equals the server truth, xid for xid")
by_label = {d["label"]: int(x) for x, d in rep["designed_truth"].items()}
chk({by_label[l] for l in rep["designed_direct_labels"]} ==
    set(inf["direct_hint_xids"]) and
    {by_label[l] for l in rep["designed_indirect_labels"]} ==
    set(inf["indirect_xids"]),
    "designed evidence split holds exactly: direct %s / indirect %s"
    % (sorted(rep["designed_direct_labels"]),
       sorted(rep["designed_indirect_labels"])))
srcrels = {s.split("(")[0] for d in inf["hint_facts"].values()
           for s in d["sources"]}
chk(len(srcrels) >= 2,
    "hint bits live on %d relations (%s) - atomicity carries facts across "
    "heaps" % (len(srcrels), sorted(srcrels)))

chk(obs["settings"]["fsync"] == "on"
    and obs["settings"]["synchronous_commit"] == "off",
    "incident configuration: fsync=on, synchronous_commit=off")
chk(obs["checkpoint"]["highest_pre_incident_xid"] < min(map(int, inf["unresolved_xids"])),
    "every unresolved xid began after the last checkpoint (xid %d)"
    % obs["checkpoint"]["highest_pre_incident_xid"])
chk(obs["checkpoint"]["clog_size_after"] == obs["capture"]["clog_file_size"],
    "the on-disk clog never grew after the checkpoint: the tail is stale, "
    "not truncated")
chk(obs["waldump_outcome_records_found"] >= n_unres,
    "pg_waldump found %d durable outcome records the on-disk clog lacks"
    % obs["waldump_outcome_records_found"])
chk(bool(obs["stale_clog_xids_verified"]),
    "raw clog bytes verified zero for every unresolved xid at capture")

# ---- the ZIP ---------------------------------------------------------------
print()
z = zipfile.ZipFile("dist/postgres_mvcc_heap_inputs_v5.zip")
names = sorted(z.namelist())
want = sorted(HEAPS + ["table_schema.json"] +
              ["pg_xact/" + n for n in slru["pg_xact"]] +
              ["pg_subtrans/" + n for n in slru["pg_subtrans"]])
chk(names == want, "ZIP contents exactly: %s" % names)
chk(not any(i.is_dir() for i in z.infolist()), "no directory entries in the ZIP")
blob = b"".join(z.read(n) for n in names)
leaks = [f for f in ["build/internal/golden.csv", "tests/expected_state.json",
                     "solution/golden_recover.py", "tests/test_outputs.py",
                     "build/pg_fixture.py",
                     "build/internal/inference_report.json"]
         if pathlib.Path(f).read_bytes() in blob]
chk(not leaks, "ZIP leaks no oracle / golden / verifier / generator content")
chk(exp["sha256"].encode() not in blob, "expected digest is not inside the ZIP")

src = pathlib.Path("solution/golden_recover.py").read_text(encoding="utf-8")
bad = [t for t in ["golden.csv", "expected_state", "internal/", "psycopg",
                   "subprocess", "socket", "urllib", "requests",
                   "tx_status"] if t in src]
chk(not bad, "oracle references only the solver-visible inputs")
chk(all(f in src for f in ("--dir", "--schema", "--out")),
    "oracle takes exactly the solver-visible inputs as arguments")

vsrc = pathlib.Path("tests/test_outputs.py").read_text(encoding="utf-8")
bad = [t for t in ["heap_pages", "ledger_entries", "account_tags", "pg_xact",
                   "pg_subtrans", "table_schema", "golden.csv", "internal/",
                   "subprocess", "os.system", "socket"] if t in vsrc]
chk(not bad, "verifier inspects only /app/recovered.csv (+ its fixture)")
chk('"/app/recovered.csv"' in vsrc, "verifier default target is /app/recovered.csv")

# ---- the prompt ------------------------------------------------------------
print()
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
  "incident stated":           ("does not record an outcome for every transaction" in prompt, True),
  "solvability stated":        ("jointly contain enough information to determine one correct result" in prompt, True),
  "all six inputs named":      (all(p in prompt for p in
                                    ("/app/heap_pages.bin",
                                     "/app/ledger_entries_heap.bin",
                                     "/app/account_tags_heap.bin",
                                     "/app/table_schema.json",
                                     "/app/pg_xact/", "/app/pg_subtrans/")), True),
  "constraints described":     ("CHECK constraints" in prompt, True),
  "only recovered.csv graded": ("Only /app/recovered.csv is evaluated" in prompt, True),
}
for k, (inp, inv) in reqs.items():
    chk(inp and inv, "prompt requirement enforced: %s%s" % (k, "" if inp else "  [NOT IN PROMPT]"))
lower = prompt.lower()
chk(all(t not in lower for t in ("enumerate", "brute force", "infer",
                                 "candidate", "hint bit", "pg_multixact",
                                 "unique survivor", "sat")),
    "prompt does not reveal the solution strategy or name retired mechanisms")
chk("state of every transaction" not in prompt
    and "Together they carry" not in prompt
    and "complete commit log" not in lower,
    "prompt never claims the commit log is complete (the v4 wording is gone)")

body = taskyaml.split("instruction: |\n", 1)[1]
dedented = "\n".join(l[2:] if l.startswith("  ") else l for l in body.split("\n")).strip()
chk(dedented == prompt_raw.strip(), "task.yaml instruction matches FINAL_PROMPT.txt verbatim")

# ---- packaging -------------------------------------------------------------
print()
required = ["VERSION_HISTORY.md", "FINAL_PROMPT.txt", "FILE_DESCRIPTION.txt",
            "DIFFICULTY_EXPLANATION.md", "GOLDEN_SOLUTION.md",
            "VERIFIER_SPEC.md", "MANIFEST.md", "INTERNAL_NOTES.md",
            "REALISM_AUDIT.md", "task.yaml", "Dockerfile", "solution.sh",
            "run-tests.sh", "docker-compose.yaml", ".gitignore"]
missing = [f for f in required if not pathlib.Path(f).exists()]
chk(not missing, "all required files present" + (" (missing %s)" % missing if missing else ""))
dirs = ["build", "solution", "tests", "artifacts", "dist", "build/negatives",
        "build/internal"]
chk(all(pathlib.Path(d).is_dir() for d in dirs), "all required directories present")
chk("All solver-facing data and database contents are synthetic and self-created for"
    in pathlib.Path("DIFFICULTY_EXPLANATION.md").read_text(encoding="utf-8"),
    "difficulty doc carries the required synthetic-data statement")

dockerfile = pathlib.Path("Dockerfile").read_text(encoding="utf-8")
for f in HEAPS + ["table_schema.json"]:
    chk("/app/" + f in dockerfile, "the image provides /app/" + f)
for d in ("pg_xact", "pg_subtrans"):
    chk("/app/%s/" % d in dockerfile, "the image provides /app/%s/" % d)
chk("pg_multixact" not in dockerfile, "the image does not copy pg_multixact/")
chk("tx_status" not in dockerfile, "the image does not copy tx_status.csv")
chk("/app/recovered.csv" in prompt_raw,
    "/app/recovered.csv is still the sole requested output")
chk(not any(pathlib.Path("dist", zz).exists()
            for zz in ("postgres_mvcc_heap_inputs.zip",
                       "postgres_mvcc_heap_inputs_v2.zip",
                       "postgres_mvcc_heap_inputs_v3.zip",
                       "postgres_mvcc_heap_inputs_v4.zip")),
    "the stale v1-v4 bundles have been removed from dist/")

scores = json.loads(pathlib.Path(
    "build/internal/negative_scores.json").read_text(encoding="utf-8"))
vals = {k: v["keys_wrong_in_total"] for k, v in scores["strategies"].items()}
chk(len(vals) == 12 and min(vals.values()) >= 1,
    "all 12 measured naive strategies are wrong (weakest: %d key(s))"
    % min(vals.values()))
chk(sum(1 for n in vals.values() if n >= 10) >= 8,
    "%d/12 strategies are wrong on >= 10 independent keys"
    % sum(1 for n in vals.values() if n >= 10))

print()
print("=" * 46)
print("FINAL AUDIT PASSED" if not fails else "AUDIT FAILURES: %s" % fails)
print("=" * 46)
sys.exit(1 if fails else 0)
