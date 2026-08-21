#!/usr/bin/env python3
"""Rebuild private/negatives/* - plausible but incomplete repairs, used to
check that the defect cannot be closed from one side alone.

    python3 build/make_negatives.py
"""
import os
import shutil

TASK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.join(TASK, "repo")
OUT = os.path.join(TASK, "private", "negatives")
SKIP = {"build", "testwork", "__pycache__"}


def rd(root, p):
    return open(os.path.join(root, p), encoding="utf-8", newline="\n").read()


def wr(root, p, s):
    open(os.path.join(root, p), "w", encoding="utf-8", newline="\n").write(s)


def raise_payload(root):
    s = rd(root, "src/internal.h").replace(
        "#define WAL_MAX_PAYLOAD KV_MAX_VALUE_LEN  /* largest payload a record carries */",
        "#define WAL_MAX_PAYLOAD PAGE_SIZE    /* a split logs the new page in full */")
    wr(root, "src/internal.h", s)


def log_images(root):
    s = rd(root, "src/btree.c")
    s = s.replace("    rc = emit(s, &r, NULL, 0, parent, rightpg);",
                  "    rc = emit(s, &r, rightpg->buf, PAGE_SIZE, parent, rightpg);")
    s = s.replace("    rc = emit(s, &r, NULL, 0, leaf, right);",
                  "    rc = emit(s, &r, right->buf, PAGE_SIZE, leaf, right);")
    wr(root, "src/btree.c", s)


def nf_btree_relog(root):
    """btree.c only: after splitting a leaf, log the moved pairs again so
    redo can put them back.  Leaves the other half of the hazard open."""
    s = rd(root, "src/btree.c")
    s = s.replace("""    uint64_t sep = LEAF_SLOTS(right)[0].key;""",
                  """    /* re-log the pairs that moved, so redo can restore them */
    for (int m = 0; m < PHDR(right)->nkeys; m++) {
        leaf_slot_t *ms = &LEAF_SLOTS(right)[m];
        wal_rec_t rr;
        memset(&rr, 0, sizeof rr);
        rr.type = WR_LEAF_PUT;
        rr.page = right_id;
        rr.key  = ms->key;
        rc = emit(s, &rr, ms->val, ms->vlen, right, NULL);
        if (rc != KV_OK) goto out;
    }

    uint64_t sep = LEAF_SLOTS(right)[0].key;""")
    wr(root, "src/btree.c", s)


def nf_recover_atomic_gate(root):
    """recover.c only: replay a split only when neither half has it yet."""
    s = rd(root, "src/recover.c")
    s = s.replace("""        if (apply_right) {
            uint64_t sep = 0;""",
                  """        if (apply_right && apply_left) {
            uint64_t sep = 0;""")
    wr(root, "src/recover.c", s)


def nf_recover_force_replay(root):
    """recover.c only: distrust the page LSNs and replay everything."""
    s = rd(root, "src/recover.c")
    s = s.replace("    *apply = (PHDR(pg)->lsn < lsn);",
                  "    *apply = 1;                 /* always replay */\n    (void)lsn;")
    wr(root, "src/recover.c", s)


def nf_payload_only(root):
    """internal.h + btree.c: the record carries the new page, but redo was
    never taught to use it."""
    raise_payload(root)
    log_images(root)


def nf_left_only(root):
    """recover.c only: protect the page that was split, and rebuild the
    sibling from whatever that page holds now."""
    s = rd(root, "src/recover.c")
    s = s.replace("""        if (apply_right) {
            uint64_t sep = 0;
            if (r->type == WR_LEAF_SPLIT)
                leaf_split(pg, right, r->aux, (int)r->arg);
            else
                int_split(pg, right, (int)r->arg, &sep);
            stamp(s, right, r->lsn);
            stamp(s, pg, r->lsn);           /* the split truncated it too */
        } else if (apply_left) {""",
                  """        if (apply_right) {
            uint64_t sep = 0;
            page_t tmp = *pg;               /* keep the page as it stands */
            if (r->type == WR_LEAF_SPLIT)
                leaf_split(&tmp, right, r->aux, (int)r->arg);
            else
                int_split(&tmp, right, (int)r->arg, &sep);
            stamp(s, right, r->lsn);
            if (apply_left) { *pg = tmp; stamp(s, pg, r->lsn); }
        } else if (apply_left) {""")
    wr(root, "src/recover.c", s)


NEGS = [
    ("nf_btree_relog", nf_btree_relog),
    ("nf_recover_atomic_gate", nf_recover_atomic_gate),
    ("nf_recover_force_replay", nf_recover_force_replay),
    ("nf_recover_protect_left", nf_left_only),
    ("nf_payload_without_redo", nf_payload_only),
]

if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    for name, fn in NEGS:
        dst = os.path.join(OUT, name)
        if os.path.exists(dst):
            shutil.rmtree(dst)
        shutil.copytree(REPO, dst,
                        ignore=lambda d, names: [n for n in names if n in SKIP])
        fn(dst)
        print("built", name)
