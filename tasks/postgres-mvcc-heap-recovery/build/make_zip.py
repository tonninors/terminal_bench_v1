#!/usr/bin/env python3
"""Build the solver-facing ZIP deterministically and inspect what went in.

The bundle contains exactly the task inputs: the raw relation blocks, the schema
and snapshot description, and the cluster's pg_xact and pg_subtrans SLRU
segments under their real directory and segment names.  Anything that could leak
the answer - the oracle, the PostgreSQL reference CSV, the expected digest, the
verifier, the generator, the tests, the internal notes, and the decoded
transaction table that v2 used to ship - is asserted to be absent.
"""
from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

TASK = Path(__file__).resolve().parent.parent

# Solver-facing members.  pg_xact/ and pg_subtrans/ keep the real SLRU directory
# names and segment file names, so the bundle unzips into the layout the prompt
# describes.
FILE_MEMBERS = ["heap_pages.bin", "table_schema.json"]
DIR_MEMBERS = ["pg_xact", "pg_subtrans"]

FIXED_TIME = (2026, 8, 20, 0, 0, 0)
OUT = TASK / "dist" / "postgres_mvcc_heap_inputs_v3.zip"
# earlier bundles carried older fixtures; shipping one now would hand out inputs
# that no longer match artifacts/ or the verifier fixture
STALE = [TASK / "dist" / "postgres_mvcc_heap_inputs.zip",
         TASK / "dist" / "postgres_mvcc_heap_inputs_v2.zip"]

FORBIDDEN_NAMES = {
    "golden.csv", "golden_recover.py", "expected_state.json", "test_outputs.py",
    "pg_fixture.py", "generate_case.py", "generation_report.json",
    "page_items.json", "solution.sh", "run-tests.sh", "internal_notes.md",
    "golden_solution.md", "verifier_spec.md", "manifest.md", "readme.md",
    "final_prompt.txt", "difficulty_explanation.md", "make_zip.py",
    "tx_status.csv", "negative_scores.json",
}
FORBIDDEN_SUBSTRINGS = ("golden", "expected", "solution", "oracle", "answer",
                        "internal", "negative", "readme", "hint", "tx_status")


def members() -> list:
    """(archive path, source path) pairs, in a fixed order."""
    out = [(n, TASK / "artifacts" / n) for n in FILE_MEMBERS]
    for d in DIR_MEMBERS:
        src = TASK / "artifacts" / d
        if not src.is_dir():
            raise SystemExit("artifacts/%s/ is missing; regenerate the fixture" % d)
        segs = sorted(p for p in src.iterdir() if p.is_file())
        if not segs:
            raise SystemExit("artifacts/%s/ holds no segment files" % d)
        out += [("%s/%s" % (d, p.name), p) for p in segs]
    return out


def main() -> int:
    OUT.parent.mkdir(exist_ok=True)
    if OUT.exists():
        OUT.unlink()
    for old in STALE:
        if old.exists():
            old.unlink()
            print("removed stale bundle %s" % old.name)

    entries = members()
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for arcname, src in entries:
            info = zipfile.ZipInfo(arcname, date_time=FIXED_TIME)
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, src.read_bytes())

    expected = sorted(a for a, _ in entries)
    with zipfile.ZipFile(OUT) as z:
        names = sorted(z.namelist())
        assert names == expected, "unexpected ZIP contents: %s" % names
        assert not any(i.is_dir() for i in z.infolist()), \
            "the ZIP contains an explicit directory entry"
        for arcname, src in entries:
            head, _, tail = arcname.rpartition("/")
            assert head == "" or head in DIR_MEMBERS, "unexpected path: %s" % arcname
            assert not tail.startswith("."), "hidden entry: %s" % arcname
            assert ".." not in arcname and not arcname.startswith("/"), arcname
            low = arcname.lower()
            assert tail.lower() not in FORBIDDEN_NAMES, \
                "forbidden member: %s" % arcname
            for bad in FORBIDDEN_SUBSTRINGS:
                assert bad not in low, "member %s looks internal (%s)" % (arcname, bad)
            assert hashlib.sha256(z.read(arcname)).hexdigest() == \
                hashlib.sha256(src.read_bytes()).hexdigest(), \
                "%s does not round-trip through the ZIP" % arcname

    print("%s" % OUT.relative_to(TASK).as_posix())
    print("  sha256 %s" % hashlib.sha256(OUT.read_bytes()).hexdigest())
    print("  %d member(s), no directory entries:" % len(entries))
    with zipfile.ZipFile(OUT) as z:
        for i in sorted(z.infolist(), key=lambda x: x.filename):
            print("    %10d  %-22s sha256 %s"
                  % (i.file_size, i.filename,
                     hashlib.sha256(z.read(i.filename)).hexdigest()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
