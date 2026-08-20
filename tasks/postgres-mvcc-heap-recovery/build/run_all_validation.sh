#!/usr/bin/env bash
# Full local validation for the postgres-mvcc-heap-recovery task.
#
#   build/run_all_validation.sh            validate the committed fixture
#   build/run_all_validation.sh --regen    regenerate it from PostgreSQL 16 first
#
# Regeneration needs Docker; everything else needs only Python 3.11 and pytest.
set -uo pipefail
cd "$(dirname "$0")/.."
TASK="$PWD"
PY=${PY:-python3}
PYTEST=${PYTEST:-$PY -m pytest}
FAIL=0
REGEN=0
[ "${1:-}" = "--regen" ] && REGEN=1

step() { printf '\n=== %s ===\n' "$1"; }
ok()   { printf '  [ OK ] %s\n' "$1"; }
bad()  { printf '  [FAIL] %s\n' "$1"; FAIL=1; }

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

step "1. inputs"
if [ "$REGEN" -eq 1 ]; then
  $PY build/generate_case.py > "$TMP/gen.json" 2>&1 \
    && ok "regenerated from a live PostgreSQL 16 server" \
    || { bad "generation failed"; tail -20 "$TMP/gen.json"; exit 1; }
else
  ok "using the committed artifacts (pass --regen to rebuild from PostgreSQL)"
fi
for f in artifacts/heap_pages.bin artifacts/tx_status.csv artifacts/table_schema.json; do
  [ -f "$f" ] || bad "missing $f"
done
sha256sum artifacts/heap_pages.bin artifacts/tx_status.csv \
          artifacts/table_schema.json build/internal/golden.csv \
          tests/expected_state.json | sed 's/^/  /'

step "2. the pages really are PostgreSQL 16 heap blocks"
$PY - <<'PYEOF' || bad "page sanity"
import struct, pathlib
h = pathlib.Path("artifacts/heap_pages.bin").read_bytes()
assert len(h) % 8192 == 0, "not a whole number of blocks"
for b in range(len(h)//8192):
    lo, up, sp, pv = struct.unpack_from("<HHHH", h, b*8192 + 12)
    assert pv & 0xFF00 == 8192 and pv & 0xFF == 4, (b, pv)
    assert 24 <= lo <= up <= sp <= 8192, (b, lo, up, sp)
print("  %d blocks, all 8192 bytes, page layout version 4" % (len(h)//8192))
PYEOF
ok "block size, count and page headers verified"

step "3. fixture properties (exclusions, HOT, snapshot boundary, traps)"
$PYTEST -q -p no:cacheprovider build/fixture_test.py \
  && ok "fixture assertions hold" || bad "fixture assertions failed"

step "4. oracle, from the three solver-visible inputs only"
$PY solution/golden_recover.py --heap artifacts/heap_pages.bin \
    --tx artifacts/tx_status.csv --schema artifacts/table_schema.json \
    --out "$TMP/recovered.csv" --report | sed 's/^/  /' || bad "oracle failed"

step "5. oracle output equals the hidden PostgreSQL reference"
$PY - "$TMP/recovered.csv" build/internal/golden.csv <<'PYEOF' || bad "oracle differs from the reference"
import csv, sys
def rows(p):
    with open(p, encoding="utf-8", newline="") as fh:
        r = [x for x in csv.reader(fh) if x]
    return r[0], sorted(r[1:])
a, b = rows(sys.argv[1]), rows(sys.argv[2])
assert a[0] == b[0], "header differs"
assert a[1] == b[1], "rows differ"
print("  %d rows identical to the state PostgreSQL reported for this snapshot" % len(a[1]))
PYEOF
ok "outcome equivalence with PostgreSQL confirmed"

step "6. verifier against the oracle output"
TB_RECOVERED_CSV="$TMP/recovered.csv" $PYTEST -q -p no:cacheprovider tests/test_outputs.py \
  && ok "verifier PASSES the oracle output" || bad "verifier rejected the oracle output"

step "7. verifier is stable across repeated runs"
for i in 1 2 3; do
  TB_RECOVERED_CSV="$TMP/recovered.csv" $PYTEST -q -p no:cacheprovider \
      tests/test_outputs.py > "$TMP/rep$i.log" 2>&1
  echo "$?" >> "$TMP/rc"
done
if [ "$(sort -u "$TMP/rc" | tr -d '\n')" = "0" ]; then ok "three runs, identical verdict"
else bad "verifier verdict varied"; fi

step "8. full PASS/FAIL matrix (negatives, alternates, malformed, missing)"
$PYTEST -q -p no:cacheprovider build/harness_test.py \
  && ok "matrix green" || bad "matrix failed"

step "9. solution.sh is in sync and self-contained"
$PY build/make_solution_sh.py >/dev/null
HEAP_PAGES="$TASK/artifacts/heap_pages.bin" TX_STATUS="$TASK/artifacts/tx_status.csv" \
  TABLE_SCHEMA="$TASK/artifacts/table_schema.json" RECOVERED_CSV="$TMP/via_sh.csv" \
  bash solution.sh >/dev/null 2>&1 || bad "solution.sh failed"
TB_RECOVERED_CSV="$TMP/via_sh.csv" $PYTEST -q -p no:cacheprovider tests/test_outputs.py \
  >/dev/null 2>&1 && ok "solution.sh output PASSES the verifier" \
  || bad "solution.sh output rejected"

step "10. rebuild and inspect the solver ZIP"
$PY build/make_zip.py | sed 's/^/  /' || bad "ZIP build/inspection failed"
sha_a=$(sha256sum dist/postgres_mvcc_heap_inputs.zip | cut -d" " -f1)
$PY build/make_zip.py >/dev/null
sha_b=$(sha256sum dist/postgres_mvcc_heap_inputs.zip | cut -d" " -f1)
[ "$sha_a" = "$sha_b" ] && ok "ZIP is byte-reproducible" || bad "ZIP is not byte-reproducible"

step "11. the ZIP leaks nothing"
$PY - <<'PYEOF' || bad "ZIP leak check failed"
import zipfile, json, pathlib
z = zipfile.ZipFile("dist/postgres_mvcc_heap_inputs.zip")
names = sorted(z.namelist())
assert names == ["heap_pages.bin", "table_schema.json", "tx_status.csv"], names
golden = pathlib.Path("build/internal/golden.csv").read_bytes()
expected = pathlib.Path("tests/expected_state.json").read_bytes()
blob = b"".join(z.read(n) for n in names)
assert golden not in blob and expected not in blob
schema = json.loads(z.read("table_schema.json"))
for forbidden in ("rows", "visible", "answer", "sha256", "expected"):
    assert forbidden not in schema, forbidden
print("  no reference answer, digest or oracle artefact inside the bundle")
PYEOF
ok "ZIP contains only the three inputs"

step "12. no network or third-party data access anywhere in the task code"
# Look for real use, not mentions: build/final_audit.py lists these names as
# things to search for, and build/pg_fixture.py legitimately talks to a local
# PostgreSQL over a unix socket.
if grep -rnE "^[[:space:]]*(import|from)[[:space:]]+(urllib|requests|socket|http|ftplib|telnetlib)\b|urlopen|requests\.(get|post)|socket\.socket|\bwget\b|\bcurl\b|https?://[a-z]" \
     build/*.py build/negatives/*.py solution/*.py tests/*.py 2>/dev/null \
   | grep -vE "build/(pg_fixture|final_audit)\.py" >/dev/null; then
  bad "network usage found in task code"
  grep -rnE "^[[:space:]]*(import|from)[[:space:]]+(urllib|requests|socket|http)\b|urlopen|requests\.(get|post)|socket\.socket|\bwget\b|\bcurl\b|https?://[a-z]" \
     build/*.py build/negatives/*.py solution/*.py tests/*.py 2>/dev/null \
   | grep -vE "build/(pg_fixture|final_audit)\.py" | sed 's/^/    /'
else
  ok "no network or third-party data access in the oracle, verifier, negatives or harness"
fi

step "12b. final audit (package claims, prompt/verifier coverage, ZIP)"
$PY build/final_audit.py | sed 's/^/  /' && ok "final audit passed" || bad "final audit failed"

step "13. the verifier reads nothing but the candidate CSV"
if grep -nE "heap_pages|tx_status|golden\.csv|internal/|subprocess|os\.system" \
     tests/test_outputs.py >/dev/null 2>&1; then
  bad "the verifier references something other than its fixture and the output"
else
  ok "verifier touches only /app/recovered.csv and tests/expected_state.json"
fi

printf '\n============================================\n'
if [ "$FAIL" -eq 0 ]; then echo "ALL LOCAL VALIDATION PASSED"; else echo "VALIDATION FAILURES PRESENT"; fi
printf '============================================\n'
exit "$FAIL"
