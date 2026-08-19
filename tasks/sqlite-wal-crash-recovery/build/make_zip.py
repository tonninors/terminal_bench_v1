#!/usr/bin/env python3
"""Build the solver-facing ZIP deterministically (fixed timestamps, fixed order).

The bundle contains exactly the two crash artifacts, at the archive root.
"""
import hashlib
import zipfile
from pathlib import Path

TASK = Path(__file__).resolve().parent.parent
MEMBERS = ["ledger.db", "ledger.db-wal"]
FIXED_TIME = (2026, 8, 19, 0, 0, 0)
OUT = TASK / "dist" / "sqlite_wal_recovery_inputs.zip"

OUT.parent.mkdir(exist_ok=True)
if OUT.exists():
    OUT.unlink()
with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
    for name in MEMBERS:
        data = (TASK / "artifacts" / name).read_bytes()
        info = zipfile.ZipInfo(name, date_time=FIXED_TIME)
        info.external_attr = 0o644 << 16
        info.compress_type = zipfile.ZIP_DEFLATED
        z.writestr(info, data)

with zipfile.ZipFile(OUT) as z:
    names = sorted(z.namelist())
    assert names == MEMBERS, names
    for name in names:
        assert "/" not in name and not name.startswith("."), name
        assert hashlib.sha256(z.read(name)).hexdigest() == hashlib.sha256(
            (TASK / "artifacts" / name).read_bytes()).hexdigest(), name
print(f"{OUT.relative_to(TASK)}  sha256 {hashlib.sha256(OUT.read_bytes()).hexdigest()}")
for name in MEMBERS:
    print(f"  {name}")
