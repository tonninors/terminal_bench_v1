#!/usr/bin/env python3
"""Regenerate the self-contained solution.sh from solution/golden_recover.py.

Terminal Bench harnesses differ in how much of a task directory they copy in
when running the oracle, so solution.sh embeds the recovery program instead of
depending on a second file being present.  solution/golden_recover.py stays the
single source of truth; this script only wraps it.
"""
from pathlib import Path

TASK = Path(__file__).resolve().parent.parent
src = (TASK / "solution" / "golden_recover.py").read_text()
assert "GOLDEN_RECOVER_EOF" not in src

out = f"""#!/bin/bash
# Oracle solution for the sqlite-wal-crash-recovery task.
#
# Reconstructs the database as of the last fully committed, internally valid WAL
# transaction using only the two crash artifacts.  No hidden expected database
# is consulted and no recovered row is hard-coded.
#
# GENERATED FILE - edit solution/golden_recover.py and rerun
# build/make_solution_sh.py instead.
set -euo pipefail

LEDGER_DB=${{LEDGER_DB:-/app/ledger.db}}
LEDGER_WAL=${{LEDGER_WAL:-/app/ledger.db-wal}}
RECOVERED_DB=${{RECOVERED_DB:-/app/recovered.db}}

PROG=$(mktemp /tmp/golden_recover.XXXXXX.py)
trap 'rm -f "$PROG"' EXIT

cat > "$PROG" <<'GOLDEN_RECOVER_EOF'
{src}GOLDEN_RECOVER_EOF

python3 "$PROG" --db "$LEDGER_DB" --wal "$LEDGER_WAL" --out "$RECOVERED_DB" --report
"""
(TASK / "solution.sh").write_text(out)
(TASK / "solution.sh").chmod(0o755)
print("solution.sh regenerated from solution/golden_recover.py")
