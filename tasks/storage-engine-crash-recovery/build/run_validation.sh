#!/usr/bin/env bash
# Validation harness: build a tree, run the repository's own suite and the
# private verifier against it, and report both verdicts on one line.
#
#   run_validation.sh <tree> [<tree> ...]
#
# A tree "passes" only if the public suite and the private verifier both
# pass.  The shipped repository and every negative must fail; only the
# reference repair may pass.
set -u

for tree in "$@"; do
    name="$(basename "$tree")"
    if ! (cd "$tree" && make clean >/dev/null 2>&1; cd "$tree" && make >/dev/null 2>&1); then
        printf '%-42s BUILD-FAILED\n' "$name"
        continue
    fi

    if (cd "$tree" && tests/run_all.sh >/dev/null 2>&1); then
        pub=pass
    else
        pub=FAIL
    fi

    pv_out="$(MINISTORE_VERIFY_DIR="/tmp/pv_$name" \
              bash "$(dirname "${BASH_SOURCE[0]}")/../private/verify.sh" \
              "$tree" 2>&1)"
    if [ $? -eq 0 ]; then
        priv=pass
    else
        priv=FAIL
    fi
    n_fail="$(printf '%s\n' "$pv_out" | grep -c '^FAIL ' || true)"

    printf '%-42s public=%-5s private=%-5s (%s private cases failed)\n' \
        "$name" "$pub" "$priv" "$n_fail"
done
