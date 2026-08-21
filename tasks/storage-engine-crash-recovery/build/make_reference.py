#!/usr/bin/env python3
"""Rebuild private/fixed (the reference repair) from the shipped tree, and
regenerate private/reference_fix.patch.

The repair makes a page that a split creates recoverable on its own: the
split record carries the new sibling as a formatted image, which needs the
log payload limit raised (internal.h), the record produced with that
payload (btree.c) and redo restoring it (recover.c).

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

NEW_SPLIT_REDO = '''    case WR_LEAF_SPLIT:
    case WR_INT_SPLIT: {
        /* The new sibling is logged as a formatted image, so neither half
         * depends on the other having survived: each is replayed only if
         * its own LSN is behind this record. */
        int apply_left = 0, apply_right = 0;
        right = page_for(s, r->aux, r->lsn, &apply_right);
        if (!right) return KV_ERR_IO;
        if (apply_right) {
            if (r->vlen != PAGE_SIZE) {
                pager_unpin(s->pg, right);
                return KV_ERR_CORRUPT;
            }
            memcpy(right->buf, payload, PAGE_SIZE);
            stamp(s, right, r->lsn);
        }
        pager_unpin(s->pg, right);

        pg = page_for(s, r->page, r->lsn, &apply_left);
        if (!pg) return KV_ERR_IO;
        if (apply_left) {
            PHDR(pg)->nkeys = (uint16_t)r->arg;
            if (r->type == WR_LEAF_SPLIT) PHDR(pg)->link = r->aux;
            stamp(s, pg, r->lsn);
        }
        pager_unpin(s->pg, pg);
        return KV_OK;
    }

'''


def rd(p):
    return open(p, encoding="utf-8", newline="\n").read()


def wr(p, s):
    open(p, "w", encoding="utf-8", newline="\n").write(s)


def build_fixed():
    if os.path.exists(FIXED):
        shutil.rmtree(FIXED)
    shutil.copytree(REPO, FIXED,
                    ignore=lambda d, names: [n for n in names if n in SKIP])

    p = os.path.join(FIXED, "src", "internal.h")
    s = rd(p).replace(
        "#define WAL_MAX_PAYLOAD KV_MAX_VALUE_LEN  /* largest payload a record carries */",
        "#define WAL_MAX_PAYLOAD PAGE_SIZE    /* a split logs the new page in full */")
    wr(p, s)

    p = os.path.join(FIXED, "src", "btree.c")
    s = rd(p)
    s = s.replace("    rc = emit(s, &r, NULL, 0, parent, rightpg);",
                  "    rc = emit(s, &r, rightpg->buf, PAGE_SIZE, parent, rightpg);")
    s = s.replace("    rc = emit(s, &r, NULL, 0, leaf, right);",
                  "    rc = emit(s, &r, right->buf, PAGE_SIZE, leaf, right);")
    wr(p, s)

    p = os.path.join(FIXED, "src", "recover.c")
    s = rd(p)
    start = s.index("    case WR_LEAF_SPLIT:")
    end = s.index("    case WR_INT_INSERT:")
    wr(p, s[:start] + NEW_SPLIT_REDO + s[end:])


def make_patch():
    parts = []
    for f in ("src/internal.h", "src/btree.c", "src/recover.c"):
        r = subprocess.run(["diff", "-u", os.path.join("repo", f),
                            os.path.join("private", "fixed", f)],
                           cwd=TASK, capture_output=True, text=True)
        parts.append(r.stdout)
    wr(PATCH, "".join(parts))


if __name__ == "__main__":
    build_fixed()
    make_patch()
    print("rebuilt private/fixed and private/reference_fix.patch")
