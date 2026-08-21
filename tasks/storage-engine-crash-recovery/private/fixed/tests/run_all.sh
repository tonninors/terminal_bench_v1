#!/usr/bin/env bash
# Runs the whole ministore test suite.  Exit status is zero only when
# every case passes.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FAILED=0

for t in test_basic.sh test_crash_recovery.sh test_transactions.sh test_idempotence.sh; do
    echo "== $t"
    if bash "$HERE/$t"; then
        :
    else
        FAILED=$((FAILED + 1))
    fi
    echo
done

if [ "$FAILED" -eq 0 ]; then
    echo "ALL TESTS PASSED"
    exit 0
fi
echo "$FAILED test file(s) reported failures"
exit 1
