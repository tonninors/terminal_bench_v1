#!/usr/bin/env bash
# Full local validation for the sqlite-wal-crash-recovery task.
#   usage: build/run_all_validation.sh
set -uo pipefail
cd "$(dirname "$0")/.."
TASK="$PWD"
PY=${PY:-python3}
PYTEST=${PYTEST:-pytest}
FAIL=0
step() { printf '\n=== %s ===\n' "$1"; }
ok()   { printf '  [ OK ] %s\n' "$1"; }
bad()  { printf '  [FAIL] %s\n' "$1"; FAIL=1; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

step "1. regenerate the case from a clean state"
rm -rf build/_work build/internal artifacts/ledger.db artifacts/ledger.db-wal tests/expected_state.json
$PY build/generate_case.py > "$TMP/gen1.json" || { bad "generator"; exit 1; }
ok "generator completed with all self-checks passing"
sha256sum artifacts/ledger.db artifacts/ledger.db-wal build/internal/golden.db \
          tests/expected_state.json > "$TMP/h1"

step "2. reproducibility: regenerate and compare hashes"
$PY build/generate_case.py > "$TMP/gen2.json" || { bad "second generator run"; exit 1; }
sha256sum artifacts/ledger.db artifacts/ledger.db-wal build/internal/golden.db \
          tests/expected_state.json > "$TMP/h2"
if diff -q "$TMP/h1" "$TMP/h2" >/dev/null; then ok "artifacts are byte-identical across runs"
else bad "generation is not reproducible"; diff "$TMP/h1" "$TMP/h2"; fi
sed 's/^/  /' "$TMP/h1"

step "3. oracle"
$PY solution/golden_recover.py --db artifacts/ledger.db --wal artifacts/ledger.db-wal \
    --out "$TMP/recovered.db" --report | sed 's/^/  /' || bad "oracle failed"
ok "oracle produced a recovered database"

step "4. oracle output is logically identical to the hidden golden state"
$PY - "$TMP/recovered.db" build/internal/golden.db <<'PYEOF' || bad "oracle output differs from golden"
import sys, sqlite3
sys.path.insert(0, "build")
import walkit as W
def fp(p):
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    d = {t: W.table_digest(c, t) for t in W.user_tables(c)}
    s = W.schema_snapshot(c)
    c.close()
    return d, s
a, b = fp(sys.argv[1]), fp(sys.argv[2])
assert a[0] == b[0], "table content differs"
assert a[1] == b[1], "schema differs"
assert open(sys.argv[1], "rb").read() != open(sys.argv[2], "rb").read(), \
    "oracle output is byte-identical to golden; the verifier would not be proving outcome equivalence"
print("  logical state matches golden; files are NOT byte-identical (as intended)")
PYEOF
ok "outcome equivalence confirmed"

step "5. verifier against the oracle output"
TB_RECOVERED_DB="$TMP/recovered.db" $PYTEST -q -p no:cacheprovider tests/test_outputs.py \
  && ok "verifier PASSES the oracle output" || bad "verifier rejected the oracle output"

step "6. negative solutions must all FAIL"
run_neg() {
  local name=$1; shift
  local out="$TMP/$name.db"
  rm -f "$out" "$out-wal" "$out-shm"
  "$@" --out "$out" > "$TMP/$name.log" 2>&1
  sed 's/^/    /' "$TMP/$name.log"
  if TB_RECOVERED_DB="$out" $PYTEST -q -p no:cacheprovider tests/test_outputs.py \
        > "$TMP/$name.verify" 2>&1; then
    bad "$name unexpectedly PASSED"
  else
    ok "$name correctly FAILED  ($(tail -1 "$TMP/$name.verify"))"
  fi
}
echo "  -- negative: main database verbatim"
run_neg copy_main $PY build/negatives/negative_copy_main.py --db artifacts/ledger.db
echo "  -- negative: ignore the WAL, salvage the main database"
run_neg ignore_wal $PY build/negatives/negative_ignore_wal.py --db artifacts/ledger.db
echo "  -- negative: replay every checksum-valid frame"
run_neg replay_all $PY build/negatives/negative_replay_all.py \
        --db artifacts/ledger.db --wal artifacts/ledger.db-wal
echo "  -- negative: trust frame headers, skip the checksum"
run_neg salt_only $PY build/negatives/negative_salt_only_scan.py \
        --db artifacts/ledger.db --wal artifacts/ledger.db-wal
echo "  -- negative: correct rows, missing schema objects"
run_neg drop_schema $PY build/negatives/negative_drop_schema_object.py --src "$TMP/recovered.db"
echo "  -- negative: only correct together with its -wal sidecar"
run_neg needs_sidecar $PY build/negatives/negative_needs_sidecar.py \
        --wrong build/internal/replay_all.db --right "$TMP/recovered.db"
echo "  -- negative: not a SQLite database"
run_neg malformed $PY build/negatives/negative_malformed.py
echo "  -- negative: missing output"
if TB_RECOVERED_DB="$TMP/does_not_exist.db" $PYTEST -q -p no:cacheprovider tests/test_outputs.py \
     >/dev/null 2>&1; then bad "missing output unexpectedly PASSED"; else ok "missing output correctly FAILED"; fi

step "7. alternate legitimate constructions must PASS"
run_pos() {
  local name=$1; shift
  local out="$TMP/$name.db"
  rm -f "$out" "$out-wal" "$out-shm"
  "$@" --out "$out" > "$TMP/$name.log" 2>&1
  sed 's/^/    /' "$TMP/$name.log"
  if TB_RECOVERED_DB="$out" $PYTEST -q -p no:cacheprovider tests/test_outputs.py \
       > "$TMP/$name.verify" 2>&1; then
    ok "$name correctly PASSED"
  else
    bad "$name unexpectedly FAILED"; tail -20 "$TMP/$name.verify"
  fi
}
run_pos alt_sql $PY build/negatives/alt_dump_restore.py --src "$TMP/recovered.db"
run_pos alt_vacuum $PY build/negatives/alt_vacuum_into.py --src "$TMP/recovered.db"

step "8. solution.sh (self-contained oracle entry point)"
$PY build/make_solution_sh.py >/dev/null
rm -f "$TMP/via_sh.db"
LEDGER_DB="$TASK/artifacts/ledger.db" LEDGER_WAL="$TASK/artifacts/ledger.db-wal" \
  RECOVERED_DB="$TMP/via_sh.db" bash solution.sh >/dev/null 2>&1 || bad "solution.sh failed"
TB_RECOVERED_DB="$TMP/via_sh.db" $PYTEST -q -p no:cacheprovider tests/test_outputs.py >/dev/null 2>&1 \
  && ok "solution.sh output PASSES the verifier" || bad "solution.sh output rejected"

step "9. full PASS/FAIL harness"
$PYTEST -q -p no:cacheprovider build/harness_test.py && ok "harness green" || bad "harness failed"

step "10. rebuild and inspect the solver ZIP"
rm -f dist/sqlite_wal_recovery_inputs.zip
mkdir -p dist
(cd artifacts && zip -X -9 ../dist/sqlite_wal_recovery_inputs.zip ledger.db ledger.db-wal) >/dev/null
$PY - <<'PYEOF' || bad "ZIP contents are wrong"
import zipfile, hashlib, pathlib
z = zipfile.ZipFile("dist/sqlite_wal_recovery_inputs.zip")
names = sorted(z.namelist())
assert names == ["ledger.db", "ledger.db-wal"], names
for n in names:
    assert "/" not in n and not n.startswith("."), n
    assert hashlib.sha256(z.read(n)).hexdigest() == \
           hashlib.sha256(pathlib.Path("artifacts", n).read_bytes()).hexdigest()
print("  ZIP holds exactly ledger.db and ledger.db-wal, byte-identical, no nesting")
PYEOF
ok "ZIP verified"

step "11. no external dependencies"
if grep -rnE "urllib|requests|http://|https://|wget|curl|socket\." \
     build/*.py build/negatives/*.py solution/*.py tests/*.py >/dev/null 2>&1; then
  bad "network usage found in task code"
else
  ok "no network or third-party data access in generator, oracle, negatives or verifier"
fi

printf '\n============================================\n'
if [ "$FAIL" -eq 0 ]; then echo "ALL LOCAL VALIDATION PASSED"; else echo "VALIDATION FAILURES PRESENT"; fi
printf '============================================\n'
exit "$FAIL"
