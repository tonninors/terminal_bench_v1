#!/usr/bin/env python3
"""Package the solver-facing repository as dist/storage-engine-inputs.zip.

The archive unpacks to a single directory named storage-engine/, so that
extracting it under /app yields /app/storage-engine.  Timestamps are
fixed, so the archive is byte reproducible.
"""
import hashlib
import os
import sys
import zipfile

TASK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.join(TASK, "repo")
OUT = os.path.join(TASK, "dist", "storage-engine-inputs.zip")
PREFIX = "storage-engine"
FIXED_TIME = (2026, 1, 1, 0, 0, 0)

SKIP_DIRS = {"build", ".git", "__pycache__", "testwork"}
SKIP_SUFFIX = (".o", ".a", ".db", ".wal", ".zip", ".patch")
FORBIDDEN = ("reference", "solution", "fixed", "negative", "answer",
             "private", "oracle", "golden")
EXEC_SUFFIX = (".sh",)


def members():
    out = []
    for dirpath, dirnames, filenames in os.walk(REPO):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            if name.endswith(SKIP_SUFFIX):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, REPO).replace(os.sep, "/")
            out.append((rel, full))
    return sorted(out)


def main():
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    files = members()
    if not files:
        sys.exit("nothing to package")
    for rel, _ in files:
        low = rel.lower()
        for bad in FORBIDDEN:
            if bad in low:
                sys.exit("refusing to package %s (matches %r)" % (rel, bad))

    if os.path.exists(OUT):
        os.remove(OUT)
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
        for rel, full in files:
            data = open(full, "rb").read()
            if b"\r\n" in data:
                sys.exit("refusing to package %s with CRLF line endings" % rel)
            info = zipfile.ZipInfo(PREFIX + "/" + rel, FIXED_TIME)
            mode = 0o755 if rel.endswith(EXEC_SUFFIX) else 0o644
            info.external_attr = (mode << 16) | 0o100000
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, data)

    blob = open(OUT, "rb").read()
    print(os.path.relpath(OUT, TASK).replace(os.sep, "/"))
    print("  sha256 %s" % hashlib.sha256(blob).hexdigest())
    print("  %d bytes, %d member(s):" % (len(blob), len(files)))
    with zipfile.ZipFile(OUT) as z:
        for i in sorted(z.infolist(), key=lambda x: x.filename):
            print("    %8d  %s" % (i.file_size, i.filename))


if __name__ == "__main__":
    main()
