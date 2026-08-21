#!/usr/bin/env python3
"""Rebuild private/negatives/* - the plausible but incomplete fixes used to
validate the task.  Each one starts from the shipped repository and applies
one repair a developer might realistically try.

    python3 build/make_negatives.py
"""
import os
import shutil

TASK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.join(TASK, "repo")
OUT = os.path.join(TASK, "private", "negatives")

BUGGY_SIG = """static int log_root_promotion(kvstore_t *s, uint32_t new_root, uint64_t sep,
                              uint32_t right_child, page_t *rootpg)"""
FIXED_SIG = """static int log_root_promotion(kvstore_t *s, uint32_t new_root,
                              uint32_t left_child, uint64_t sep,
                              uint32_t right_child, page_t *rootpg)"""


def read(p):
    return open(p, encoding="utf-8", newline="\n").read()


def write(p, s):
    open(p, "w", encoding="utf-8", newline="\n").write(s)


def apply_record_fix(root):
    """The correct logging repair; base for the later negatives."""
    p = os.path.join(root, "src", "btree.c")
    s = read(p)
    s = s.replace(BUGGY_SIG, FIXED_SIG)
    s = s.replace(
        "    r.aux  = pager_meta(s->pg)->root;      /* left child of the new root */",
        "    r.aux  = left_child;                   /* left child of the new root */")
    s = s.replace(
        "    int rc = log_root_promotion(s, new_id, sep, right_child, nr);",
        "    int rc = log_root_promotion(s, new_id, old_root, sep, right_child, nr);")
    write(p, s)


def nf1(root):
    """Reorders bookkeeping only: looks like an ordering fix, changes nothing."""
    p = os.path.join(root, "src", "btree.c")
    s = read(p)
    s = s.replace("""    m->root = new_id;
    m->height++;
    pager_meta_dirty(s->pg);

    int rc = log_root_promotion(s, new_id, sep, right_child, nr);""",
                  """    m->root = new_id;
    pager_meta_dirty(s->pg);

    int rc = log_root_promotion(s, new_id, sep, right_child, nr);
    m->height++;""")
    write(p, s)


def nf2(root):
    """Patches recovery, assuming the tree always grew out of page 1."""
    p = os.path.join(root, "src", "recover.c")
    s = read(p)
    s = s.replace("""        if (apply) {
            node_init(pg, PT_INTERNAL);
            int_set_leftmost(pg, r->aux);""",
                  """        if (apply) {
            uint32_t left = (r->aux == r->page) ? 1u : r->aux;
            node_init(pg, PT_INTERNAL);
            int_set_leftmost(pg, left);""")
    write(p, s)


def nf3(root):
    """Correct record, but redo is forced to re-apply every record."""
    apply_record_fix(root)
    p = os.path.join(root, "src", "recover.c")
    s = read(p)
    s = s.replace("    *apply = (PHDR(pg)->lsn < lsn);",
                  "    *apply = 1;                 /* always replay */\n    (void)lsn;")
    write(p, s)


def nf4(root):
    """Correct record, but recovery no longer republishes the new root."""
    apply_record_fix(root)
    p = os.path.join(root, "src", "recover.c")
    s = read(p)
    s = s.replace("""        pager_unpin(s->pg, pg);
        pager_meta(s->pg)->root = r->page;
        pager_meta_dirty(s->pg);
        return KV_OK;""",
                  """        pager_unpin(s->pg, pg);
        return KV_OK;""")
    write(p, s)


def nf5(root):
    """Special-cases the first promotion: patches the first failing test."""
    p = os.path.join(root, "src", "btree.c")
    s = read(p)
    s = s.replace(
        "    r.aux  = pager_meta(s->pg)->root;      /* left child of the new root */",
        "    /* the first promotion always grows out of the original root page */\n"
        "    r.aux  = (pager_meta(s->pg)->height == 2) ? 1u\n"
        "                                              : pager_meta(s->pg)->root;")
    write(p, s)


NEGS = [
    ("nf1_reorder_bookkeeping", nf1),
    ("nf2_recovery_assumes_first_root", nf2),
    ("nf3_replay_everything", nf3),
    ("nf4_recovery_keeps_old_root", nf4),
    ("nf5_only_first_promotion", nf5),
]

SKIP = {"build", "testwork", "__pycache__"}


def copy_repo(dst):
    shutil.copytree(REPO, dst,
                    ignore=lambda d, names: [n for n in names if n in SKIP])


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    for name, fn in NEGS:
        dst = os.path.join(OUT, name)
        if os.path.exists(dst):
            shutil.rmtree(dst)
        copy_repo(dst)
        fn(dst)
        print("built", name)
