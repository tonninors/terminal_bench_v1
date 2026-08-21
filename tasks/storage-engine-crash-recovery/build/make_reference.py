#!/usr/bin/env python3
"""Rebuild private/fixed (the reference repair) from the shipped tree and
regenerate private/reference_fix.patch.

The shipped engine keeps the log inside its budget from inside the write
path, before the transaction has committed.  That checkpoint flushes the
pages the unfinished transaction has already touched and then recycles
the log, so a crash in that window leaves work on disk that was never
committed and no record that would let recovery tell.  The repair is to
let the log budget be enforced only where no transaction is in flight.

    python3 build/make_reference.py
"""
import os
import shutil
import subprocess

TASK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.join(TASK, "repo")
FIXED = os.path.join(TASK, "private", "fixed")
PATCH = os.path.join(TASK, "private", "reference_fix.patch")
SKIP = {"build", "testwork", "__pycache__"}


def rd(p):
    return open(p, encoding="utf-8", newline="\n").read()


def wr(p, s):
    open(p, "w", encoding="utf-8", newline="\n").write(s)


def build_fixed():
    if os.path.exists(FIXED):
        shutil.rmtree(FIXED)
    shutil.copytree(REPO, FIXED,
                    ignore=lambda d, names: [n for n in names if n in SKIP])

    p = os.path.join(FIXED, "src", "kvstore.c")
    s = rd(p)

    # the budget may only be enforced once the transaction is durable
    s = s.replace("""    rc = btree_put(s, key, val, len);
    if (rc != KV_OK) return rc;
    rc = keep_log_bounded(s);
    if (rc != KV_OK) return rc;
    return txn_commit(s);""",
                  """    rc = btree_put(s, key, val, len);
    if (rc != KV_OK) return rc;
    rc = txn_commit(s);
    if (rc != KV_OK) return rc;
    return keep_log_bounded(s);""")
    s = s.replace("""    rc = btree_del(s, key);
    if (rc != KV_OK) return rc;            /* nothing logged but BEGIN */
    rc = keep_log_bounded(s);
    if (rc != KV_OK) return rc;
    return txn_commit(s);""",
                  """    rc = btree_del(s, key);
    if (rc != KV_OK) return rc;            /* nothing logged but BEGIN */
    rc = txn_commit(s);
    if (rc != KV_OK) return rc;
    return keep_log_bounded(s);""")
    s = s.replace("""/* The log is not allowed to grow without bound: once it passes
 * MS_LOG_LIMIT the engine takes a checkpoint, which flushes the pages and
 * reclaims the records the data file no longer needs. */""",
                  """/* The log is not allowed to grow without bound: once it passes
 * MS_LOG_LIMIT the engine takes a checkpoint, which flushes the pages and
 * reclaims the records the data file no longer needs.  A checkpoint
 * publishes whatever the pages currently hold and then drops the records
 * that describe it, so it may only be taken when no transaction is part
 * way through: between operations, never inside one. */""")
    wr(p, s)


def make_patch():
    r = subprocess.run(["diff", "-u", "repo/src/kvstore.c",
                        "private/fixed/src/kvstore.c"],
                       cwd=TASK, capture_output=True, text=True)
    wr(PATCH, r.stdout)


if __name__ == "__main__":
    build_fixed()
    make_patch()
    print("rebuilt private/fixed and private/reference_fix.patch")
