#!/usr/bin/env bash
# Full validation of the ministore input package.
#
#   build/run_validation.sh
#
# Rebuilds the solver ZIP, extracts it as /app/storage-engine inside a
# throwaway container, and checks:
#   1. the buggy baseline builds warning free and passes the normal tests
#   2. the buggy baseline fails the crash and idempotence tests
#   3. the private reference fix passes everything
#   4. every plausible incomplete fix still fails
#   5. nothing private is inside the archive
# Requires docker and python3.
set -u
cd "$(dirname "$0")/.."
TASK="$PWD"
IMG=debian:bookworm
C=ministore-validate
FAIL=0

ok()  { printf '  [ OK ] %s\n' "$1"; }
bad() { printf '  [FAIL] %s\n' "$1"; FAIL=1; }

step() { printf '\n=== %s ===\n' "$1"; }

step "1. rebuild the archive"
python3 build/make_zip.py || { bad "packaging"; exit 1; }

step "2. start a clean container"
docker rm -f "$C" >/dev/null 2>&1
docker run -d --name "$C" "$IMG" sleep infinity >/dev/null || exit 1
trap 'docker rm -f "$C" >/dev/null 2>&1' EXIT
docker exec "$C" bash -c \
    "apt-get update -qq >/dev/null 2>&1 && \
     apt-get install -y -qq build-essential unzip patch >/dev/null 2>&1" \
    || { bad "toolchain install"; exit 1; }
docker cp dist/storage-engine-inputs.zip "$C:/tmp/z.zip" >/dev/null
docker cp private/reference_fix.patch "$C:/tmp/ref.patch" >/dev/null

run_suite() {   # run_suite <label> -> prints "basic crash idem"
    docker exec "$C" bash -c '
        cd /app/storage-engine && make clean >/dev/null 2>&1
        make >/dev/null 2>&1 || { echo "BUILD_FAILED"; exit 1; }
        bash tests/run_all.sh 2>&1 | sed -n "s/^---- \(.*\): \([0-9]*\) passed, \([0-9]*\) failed/\1 \2 \3/p"'
}

extract() {
    docker exec "$C" bash -c "rm -rf /app && mkdir -p /app && cd /app && unzip -q /tmp/z.zip"
}

step "3. buggy baseline"
extract
out="$(run_suite)"
echo "$out" | sed 's/^/    /'
echo "$out" | grep -q "test_basic.sh 26 0" \
    && ok "normal operation passes on the shipped tree" \
    || bad "normal operation should pass on the shipped tree"
echo "$out" | grep -q "test_crash_recovery.sh .* [1-9]" \
    && ok "crash recovery tests fail as intended" \
    || bad "crash recovery tests should fail on the shipped tree"
echo "$out" | grep -q "test_idempotence.sh .* [1-9]" \
    && ok "idempotence tests fail as intended" \
    || bad "idempotence tests should fail on the shipped tree"

step "4. private reference fix"
docker exec "$C" bash -c '
    cd /app/storage-engine
    sed "s|^--- repo/|--- a/|; s|^+++ private/fixed/|+++ b/|" /tmp/ref.patch > /tmp/r.patch
    patch -p1 < /tmp/r.patch >/dev/null'
out="$(run_suite)"
echo "$out" | sed 's/^/    /'
if [ "$(echo "$out" | awk '{s+=$3} END {print s+0}')" -eq 0 ]; then
    ok "reference fix passes every test"
else
    bad "reference fix must pass every test"
fi

step "5. incomplete fixes"
for neg in private/negatives/*/; do
    name="$(basename "$neg")"
    docker exec "$C" bash -c "rm -rf /app && mkdir -p /app" >/dev/null
    docker cp "$neg" "$C:/app/storage-engine" >/dev/null
    out="$(run_suite)"
    total_fail="$(echo "$out" | awk '{s+=$3} END {print s+0}')"
    basic_fail="$(echo "$out" | awk '$1 ~ /basic/ {print $3+0}')"
    if [ "${total_fail:-0}" -gt 0 ] && [ "${basic_fail:-1}" -eq 0 ]; then
        ok "$name still fails the suite (${total_fail} cases) and keeps normal ops working"
    else
        bad "$name did not behave as an incomplete fix (fails=${total_fail}, basic_fails=${basic_fail})"
    fi
done

step "6. archive hygiene"
if python3 - dist/storage-engine-inputs.zip << 'PY'
import re, sys, zipfile
bad = re.compile(r"patch|fixed|reference|negative|private|solution|answer|oracle|golden", re.I)
names = zipfile.ZipFile(sys.argv[1]).namelist()
leaks = [n for n in names if bad.search(n)]
assert not leaks, leaks
assert all(n.startswith("storage-engine/") for n in names), names
sys.exit(0)
PY
then ok "archive holds only the solver tree, rooted at storage-engine/"
else bad "archive hygiene"
fi

printf '\n============================================\n'
[ "$FAIL" -eq 0 ] && echo "VALIDATION PASSED" || echo "VALIDATION FAILURES PRESENT"
printf '============================================\n'
exit "$FAIL"
