#!/usr/bin/env python3
"""Regenerate the self-contained solution.sh from solution/golden_recover.py.

solution.sh has to run inside the task container with nothing but a Python 3
interpreter, so the oracle is embedded verbatim rather than referenced.
"""
from pathlib import Path

TASK = Path(__file__).resolve().parent.parent
ORACLE = TASK / "solution" / "golden_recover.py"
OUT = TASK / "solution.sh"

MARKER = "GOLDEN_RECOVER_EOF"

TEMPLATE = """#!/bin/bash
# Oracle solution for the postgres-mvcc-heap-recovery task.
#
# Reconstructs the MVCC-visible rows using only the solver-visible inputs: the
# raw heap blocks, the cluster's pg_xact and pg_subtrans segments, and the
# schema/snapshot file.
# No hidden expected answer is consulted and no recovered row is hard-coded.
#
# GENERATED FILE - edit solution/golden_recover.py and rerun
# build/make_solution_sh.py instead.
set -euo pipefail

HEAP_PAGES=${HEAP_PAGES:-/app/heap_pages.bin}
PG_XACT=${PG_XACT:-/app/pg_xact}
PG_SUBTRANS=${PG_SUBTRANS:-/app/pg_subtrans}
TABLE_SCHEMA=${TABLE_SCHEMA:-/app/table_schema.json}
RECOVERED_CSV=${RECOVERED_CSV:-/app/recovered.csv}

PROG=$(mktemp /tmp/golden_recover.XXXXXX.py)
trap 'rm -f "$PROG"' EXIT

cat > "$PROG" <<'@MARKER@'
@BODY@
@MARKER@

python3 "$PROG" --heap "$HEAP_PAGES" --pg-xact "$PG_XACT" \\
                --pg-subtrans "$PG_SUBTRANS" --schema "$TABLE_SCHEMA" \\
                --out "$RECOVERED_CSV" --report
"""


def main() -> int:
    body = ORACLE.read_text(encoding="utf-8")
    if MARKER in body:
        raise SystemExit("the oracle body contains the heredoc marker")
    text = TEMPLATE.replace("@MARKER@", MARKER).replace("@BODY@", body.rstrip("\n"))
    OUT.write_text(text, encoding="utf-8", newline="\n")
    print("wrote", OUT.relative_to(TASK))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
