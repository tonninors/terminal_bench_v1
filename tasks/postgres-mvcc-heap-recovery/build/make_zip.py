#!/usr/bin/env python3
"""Build the solver-facing ZIP deterministically and inspect what went in.

The bundle contains exactly the three task inputs, at the archive root, with no
containing folder.  Anything that could leak the answer - the oracle, the
PostgreSQL reference CSV, the expected digest, the verifier, the generator, the
tests and the internal notes - is asserted to be absent.
"""
from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

TASK = Path(__file__).resolve().parent.parent
MEMBERS = ["heap_pages.bin", "table_schema.json", "tx_status.csv"]
FIXED_TIME = (2026, 8, 20, 0, 0, 0)
OUT = TASK / "dist" / "postgres_mvcc_heap_inputs.zip"

FORBIDDEN_NAMES = {
    "golden.csv", "golden_recover.py", "expected_state.json", "test_outputs.py",
    "pg_fixture.py", "generate_case.py", "generation_report.json",
    "page_items.json", "solution.sh", "run-tests.sh", "INTERNAL_NOTES.md",
    "GOLDEN_SOLUTION.md", "VERIFIER_SPEC.md", "MANIFEST.md", "README.md",
    "FINAL_PROMPT.txt", "DIFFICULTY_EXPLANATION.md", "make_zip.py",
}
FORBIDDEN_SUBSTRINGS = ("golden", "expected", "solution", "oracle", "answer",
                        "internal", "negative", "test", "readme", "hint")


def main() -> int:
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
        assert names == sorted(MEMBERS), f"unexpected ZIP contents: {names}"
        for name in names:
            assert "/" not in name and "\\" not in name, f"nested path: {name}"
            assert not name.startswith("."), f"hidden entry: {name}"
            low = name.lower()
            assert low not in FORBIDDEN_NAMES, f"forbidden member: {name}"
            for bad in FORBIDDEN_SUBSTRINGS:
                assert bad not in low, f"member {name} looks internal ({bad})"
            got = hashlib.sha256(z.read(name)).hexdigest()
            want = hashlib.sha256(
                (TASK / "artifacts" / name).read_bytes()).hexdigest()
            assert got == want, f"{name} does not round-trip through the ZIP"
        infolist = z.infolist()
        assert not any(i.is_dir() for i in infolist), "the ZIP contains a folder"

    print(f"{OUT.relative_to(TASK).as_posix()}")
    print(f"  sha256 {hashlib.sha256(OUT.read_bytes()).hexdigest()}")
    print(f"  {len(MEMBERS)} member(s), no directory entries:")
    with zipfile.ZipFile(OUT) as z:
        for i in sorted(z.infolist(), key=lambda x: x.filename):
            print("    %10d  %-20s sha256 %s"
                  % (i.file_size, i.filename,
                     hashlib.sha256(z.read(i.filename)).hexdigest()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
