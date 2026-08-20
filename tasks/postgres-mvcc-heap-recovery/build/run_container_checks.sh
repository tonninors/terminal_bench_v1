#!/usr/bin/env bash
# Container-level equivalents of the Terminal Bench oracle and nop checks.
#
# The official `tb` CLI is not available in this environment, so this script
# does the same two things directly against the real task image:
#
#   nop    : build the image, run no agent at all, run run-tests.sh  -> must FAIL
#   oracle : build the image, run solution.sh, run run-tests.sh      -> must PASS
#
# Requires Docker.  Usage: build/run_container_checks.sh
set -uo pipefail
cd "$(dirname "$0")/.."
IMAGE=postgres-mvcc-heap-recovery
NAME=tb-mvcc-check
PY=${PY:-python3}
WORK=build/_work
mkdir -p "$WORK"
FAIL=0
ok()  { printf '  [ OK ] %s\n' "$1"; }
bad() { printf '  [FAIL] %s\n' "$1"; FAIL=1; }

printf '\n=== building the task image ===\n'
docker build -q -t "$IMAGE" . || { bad "docker build"; exit 1; }
ok "image built"

docker rm -f "$NAME" >/dev/null 2>&1
docker run -d --name "$NAME" --network none -w /app "$IMAGE" sleep infinity >/dev/null \
  || { bad "docker run"; exit 1; }
trap 'docker rm -f "$NAME" >/dev/null 2>&1' EXIT

printf '\n=== what the solver sees in /app ===\n'
docker exec "$NAME" ls -la /app | sed 's/^/  /'

# the harness copies the tests in only after the agent has finished
docker cp tests "$NAME:/app/tests" >/dev/null
docker cp run-tests.sh "$NAME:/app/run-tests.sh" >/dev/null
docker cp solution.sh "$NAME:/tmp/solution.sh" >/dev/null

printf '\n=== the decoded transaction table is gone ===\n'
if docker exec "$NAME" test -e /app/tx_status.csv; then
  bad "tx_status.csv is present in the image"
else
  ok "no /app/tx_status.csv; state must come from pg_xact and pg_subtrans"
fi
for seg in pg_xact pg_subtrans; do
  n=$(docker exec "$NAME" sh -c "ls -1 /app/$seg | wc -l")
  if [ "$n" -ge 1 ]; then ok "/app/$seg/ ships $n segment file(s)"
  else bad "/app/$seg/ is empty"; fi
done

printf '\n=== nop: no agent ran, /app/recovered.csv absent ===\n'
docker exec "$NAME" bash -c 'rm -f /app/recovered.csv; bash /app/run-tests.sh' \
  >$WORK/nop.log 2>&1
rc=$?
tail -2 $WORK/nop.log | sed 's/^/  /'
[ "$rc" -ne 0 ] && ok "nop correctly FAILED (exit $rc)" || bad "nop unexpectedly PASSED"

printf '\n=== oracle: solution.sh, then run-tests.sh ===\n'
docker exec "$NAME" bash /tmp/solution.sh >$WORK/oracle.log 2>&1 \
  || { bad "solution.sh failed inside the container"; tail -5 $WORK/oracle.log; }
grep -E '"visible_rows"|"blocks"|"physical_tuples"' $WORK/oracle.log | sed 's/^/  /'
docker exec "$NAME" bash /app/run-tests.sh >$WORK/verify.log 2>&1
rc=$?
tail -2 $WORK/verify.log | sed 's/^/  /'
[ "$rc" -eq 0 ] && ok "oracle correctly PASSED" || bad "oracle unexpectedly FAILED"

printf '\n=== the in-container answer matches the host oracle byte for byte ===\n'
docker exec "$NAME" sha256sum /app/recovered.csv | sed 's/^/  /'
$PY solution/golden_recover.py --heap artifacts/heap_pages.bin \
    --pg-xact artifacts/pg_xact --pg-subtrans artifacts/pg_subtrans \
    --schema artifacts/table_schema.json \
    --out "$WORK/host_recovered.csv" >/dev/null \
  || bad "the host oracle run failed"
host=$(sha256sum "$WORK/host_recovered.csv" | cut -d' ' -f1)
cont=$(docker exec "$NAME" sha256sum /app/recovered.csv | cut -d' ' -f1)
[ "$host" = "$cont" ] && ok "identical ($host)" || bad "container and host answers differ"

printf '\n=== the container has no PostgreSQL and no network ===\n'
docker exec "$NAME" bash -c 'command -v postgres psql pg_filedump || true' \
  >$WORK/pg.log 2>&1
[ -s $WORK/pg.log ] && bad "a PostgreSQL binary is present: $(cat $WORK/pg.log)" \
                   || ok "no postgres, psql or pg_filedump in the image"

printf '\n============================================\n'
if [ "$FAIL" -eq 0 ]; then echo "CONTAINER CHECKS PASSED"; else echo "CONTAINER CHECKS FAILED"; fi
printf '============================================\n'
exit "$FAIL"
