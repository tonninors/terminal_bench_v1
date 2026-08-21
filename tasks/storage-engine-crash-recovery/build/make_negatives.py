#!/usr/bin/env python3
"""Build the negative trees: plausible-but-incomplete repairs, and the
generic workarounds that a solver might reach for instead of doing the
work.  Every one of them has to fail.

Trees whose name starts with `bypass_` are built from the shipped
(defective) repository: they are the cheap generic moves.  Trees whose
name starts with `neg_` are built from the reference repair and then
degraded: they are the honest but incomplete attempts.

    python3 build/make_negatives.py
"""
import os
import shutil

TASK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.join(TASK, "repo")
FIXED = os.path.join(TASK, "private", "fixed")
OUT = os.path.join(TASK, "private", "negatives")
SKIP = {"build", "testwork", "__pycache__"}


def rd(p):
    return open(p, encoding="utf-8", newline="\n").read()


def wr(p, s):
    open(p, "w", encoding="utf-8", newline="\n").write(s)


def tree(name, src):
    d = os.path.join(OUT, name)
    if os.path.exists(d):
        shutil.rmtree(d)
    shutil.copytree(src, d,
                    ignore=lambda dd, names: [n for n in names if n in SKIP])
    return d


def sub(d, path, old, new):
    p = os.path.join(d, path)
    s = rd(p)
    if old not in s:
        raise SystemExit("anchor missing in %s/%s:\n%s" % (
            os.path.basename(d), path, old[:160]))
    wr(p, s.replace(old, new, 1))


def build():
    if os.path.exists(OUT):
        shutil.rmtree(OUT)
    os.makedirs(OUT)

    # -- A: keep the log budget out of the write path, so the only
    #       checkpoint is the one between transactions
    d = tree("bypass_A_checkpoint_between_ops", REPO)
    sub(d, "src/kvstore.c",
        """    if (own) return kv_commit(s);
    return keep_log_bounded(s);
}

int kv_delete""",
        """    if (own) return kv_commit(s);
    return KV_OK;
}

int kv_delete""")
    sub(d, "src/kvstore.c",
        """    if (own) return kv_commit(s);
    return keep_log_bounded(s);
}

int kv_get""",
        """    if (own) return kv_commit(s);
    return KV_OK;
}

int kv_get""")

    # -- B: never recycle the log
    d = tree("bypass_B_no_recycling", REPO)
    sub(d, "src/wal.c",
        """int wal_truncate(wal_t *w)
{
    if (ftruncate(w->fd, 0) != 0) return KV_ERR_IO;
    w->end = 0;
    return wal_sync(w);
}""",
        """int wal_truncate(wal_t *w)
{
    return wal_sync(w);            /* keep every record for ever */
}""")

    # -- C: replay everything in the log, committed or not
    d = tree("bypass_C_replay_everything", REPO)
    sub(d, "src/recover.c",
        "        if (!txnset_has(&committed, r.txn)) continue;\n",
        "")

    # -- D: force every dirty page out at commit
    d = tree("bypass_D_flush_at_commit", REPO)
    sub(d, "src/kvstore.c",
        """    int rc = txn_commit(s);
    if (rc != KV_OK) return rc;
    s->in_txn = 0;
    txn_forget(s);
    return keep_log_bounded(s);""",
        """    int rc = txn_commit(s);
    if (rc != KV_OK) return rc;
    rc = pager_flush_all(s->pg);
    if (rc == KV_OK) rc = pager_sync(s->pg);
    if (rc != KV_OK) return rc;
    s->in_txn = 0;
    txn_forget(s);
    return keep_log_bounded(s);""")

    # -- E: a buffer pool big enough to hold anything the tests do
    d = tree("bypass_E_big_buffer_pool", REPO)
    sub(d, "src/pager.c", "#define NFRAMES 32", "#define NFRAMES 8192")

    # -- F: no steal - a big pool, and neither eviction nor a checkpoint
    #       may write a page the running transaction has touched
    d = tree("bypass_F_no_steal", REPO)
    sub(d, "src/pager.c", "#define NFRAMES 32", "#define NFRAMES 8192")
    sub(d, "src/pager.c",
        """static int frame_writeback(pager_t *p, page_t *f)
{
    if (!f->dirty) return KV_OK;""",
        """int ms_pin_dirty_pages = 0;   /* set while a transaction is running */

static int frame_writeback(pager_t *p, page_t *f)
{
    if (!f->dirty) return KV_OK;
    if (ms_pin_dirty_pages) return KV_OK;    /* hold it until commit */""")
    sub(d, "src/kvstore.c",
        '#include "internal.h"',
        '#include "internal.h"\n\nextern int ms_pin_dirty_pages;')
    sub(d, "src/kvstore.c",
        """int kv_commit(kvstore_t *s)
{
    if (!s || !s->in_txn) return KV_ERR_INVAL;""",
        """int kv_commit(kvstore_t *s)
{
    if (!s || !s->in_txn) return KV_ERR_INVAL;
    ms_pin_dirty_pages = 0;""")
    sub(d, "src/kvstore.c",
        """    int rc = txn_begin(s);
    if (rc != KV_OK) return rc;
    s->in_txn = 1;""",
        """    int rc = txn_begin(s);
    if (rc != KV_OK) return rc;
    ms_pin_dirty_pages = 1;
    s->in_txn = 1;""")
    sub(d, "src/kvstore.c",
        """int kv_abort(kvstore_t *s)
{
    if (!s || !s->in_txn) return KV_ERR_INVAL;""",
        """int kv_abort(kvstore_t *s)
{
    if (!s || !s->in_txn) return KV_ERR_INVAL;
    ms_pin_dirty_pages = 0;""")
    # -- F2: a fair no-steal engine.  A pool big enough to hold any
    #        transaction the tests run, dirty pages of the running
    #        transaction pinned in memory, and - because pinning them is
    #        pointless if the log that describes them is thrown away - no
    #        recycling while a transaction is live.  This is the strongest
    #        form of "just keep it all in memory until commit".
    d = tree("bypass_F2_fair_no_steal", REPO)
    sub(d, "src/pager.c", "#define NFRAMES 32", "#define NFRAMES 8192")
    sub(d, "src/pager.c",
        """static int frame_writeback(pager_t *p, page_t *f)
{
    if (!f->dirty) return KV_OK;""",
        """int ms_txn_live = 0;          /* set while a transaction is running */

static int frame_writeback(pager_t *p, page_t *f)
{
    if (!f->dirty) return KV_OK;
    if (ms_txn_live) return KV_OK;           /* hold it until commit */""")
    sub(d, "src/kvstore.c",
        '#include "internal.h"',
        '#include "internal.h"\n\nextern int ms_txn_live;')
    sub(d, "src/kvstore.c",
        """    int rc = txn_begin(s);
    if (rc != KV_OK) return rc;
    s->in_txn = 1;""",
        """    int rc = txn_begin(s);
    if (rc != KV_OK) return rc;
    ms_txn_live = 1;
    s->in_txn = 1;""")
    sub(d, "src/kvstore.c",
        """int kv_commit(kvstore_t *s)
{
    if (!s || !s->in_txn) return KV_ERR_INVAL;""",
        """int kv_commit(kvstore_t *s)
{
    if (!s || !s->in_txn) return KV_ERR_INVAL;
    ms_txn_live = 0;""")
    sub(d, "src/kvstore.c",
        """int kv_abort(kvstore_t *s)
{
    if (!s || !s->in_txn) return KV_ERR_INVAL;""",
        """int kv_abort(kvstore_t *s)
{
    if (!s || !s->in_txn) return KV_ERR_INVAL;
    ms_txn_live = 0;""")
    # a checkpoint that cannot flush must not recycle either
    sub(d, "src/kvstore.c",
        """    rc = wal_truncate(s->wal);
    if (rc == KV_OK) ms_crash_hook("checkpoint");
    return rc;""",
        """    if (s->in_txn) {
        ms_crash_hook("checkpoint");
        return KV_OK;
    }
    rc = wal_truncate(s->wal);
    if (rc == KV_OK) ms_crash_hook("checkpoint");
    return rc;""")

    # -- F3: no-steal done properly.  A pool big enough to hold any
    #        transaction the tests run; dirty pages of the running
    #        transaction never written; and a checkpoint that simply does
    #        not happen while a transaction is live, so it can neither
    #        publish those pages nor claim in the meta page to have
    #        absorbed log it has not absorbed.  Nothing an unfinished
    #        transaction does reaches the disk at all, so there is
    #        nothing to undo.  This is the strongest honest form of
    #        "keep it in memory until commit"; its costs are that memory
    #        grows with the size of a transaction and the log outgrows
    #        its budget.
    d = tree("bypass_F3_no_steal_correct", REPO)
    sub(d, "src/pager.c", "#define NFRAMES 32", "#define NFRAMES 8192")
    sub(d, "src/pager.c",
        """static int frame_writeback(pager_t *p, page_t *f)
{
    if (!f->dirty) return KV_OK;""",
        """int ms_txn_live = 0;          /* set while a transaction is running */

static int frame_writeback(pager_t *p, page_t *f)
{
    if (!f->dirty) return KV_OK;
    if (ms_txn_live) return KV_OK;           /* hold it until commit */""")
    sub(d, "src/kvstore.c",
        '#include "internal.h"',
        '#include "internal.h"\n\nextern int ms_txn_live;')
    sub(d, "src/kvstore.c",
        """    int rc = txn_begin(s);
    if (rc != KV_OK) return rc;
    s->in_txn = 1;""",
        """    int rc = txn_begin(s);
    if (rc != KV_OK) return rc;
    ms_txn_live = 1;
    s->in_txn = 1;""")
    sub(d, "src/kvstore.c",
        """int kv_commit(kvstore_t *s)
{
    if (!s || !s->in_txn) return KV_ERR_INVAL;""",
        """int kv_commit(kvstore_t *s)
{
    if (!s || !s->in_txn) return KV_ERR_INVAL;
    ms_txn_live = 0;""")
    sub(d, "src/kvstore.c",
        """int kv_abort(kvstore_t *s)
{
    if (!s || !s->in_txn) return KV_ERR_INVAL;""",
        """int kv_abort(kvstore_t *s)
{
    if (!s || !s->in_txn) return KV_ERR_INVAL;
    ms_txn_live = 0;""")
    # a checkpoint that cannot flush must not happen at all: advancing
    # checkpoint_lsn over pages still in memory would lose committed work
    sub(d, "src/kvstore.c",
        """    if (!s) return KV_ERR_INVAL;
    rc = pager_flush_all(s->pg);""",
        """    if (!s) return KV_ERR_INVAL;
    if (s->in_txn) return KV_OK;         /* not while a transaction runs */
    rc = pager_flush_all(s->pg);""")

    # -- neg 1: the before-images are written to the log, and recovery
    #           never looks at them
    d = tree("neg_wal_records_only", FIXED)
    sub(d, "src/recover.c",
        """    rc = recover_undo(s, &committed, &ended);
    free(committed.ids);
    free(ended.ids);
    if (rc != KV_OK) return rc;""",
        """    free(committed.ids);
    free(ended.ids);""")
    # silence the now-unused function
    sub(d, "src/recover.c",
        "static int recover_undo(kvstore_t *s, const txnset_t *committed,",
        "__attribute__((unused))\n"
        "static int recover_undo(kvstore_t *s, const txnset_t *committed,")

    # -- neg 2: only leaf pages get a durable before-image
    d = tree("neg_leaf_pages_only", FIXED)
    sub(d, "src/kvstore.c",
        """    {
        wal_rec_t r;
        uint64_t lsn = 0;
        int rc;
        memset(&r, 0, sizeof r);""",
        """    if (pg->id != META_PAGE && PHDR(pg)->type == PT_LEAF) {
        wal_rec_t r;
        uint64_t lsn = 0;
        int rc;
        memset(&r, 0, sizeof r);""")

    # -- neg 3: the rollback puts the keys and values back but leaves the
    #           shape of the tree as the interrupted transaction left it
    d = tree("neg_data_not_structure", FIXED)
    sub(d, "src/recover.c",
        """        page_t *pg = pager_ensure(s->pg, r.page);
        if (!pg) goto io;
        undo_restore(pg, payload);""",
        """        page_t *pg = pager_ensure(s->pg, r.page);
        if (!pg) goto io;
        if (((const page_hdr_t *)payload)->type != PT_LEAF) {
            pager_unpin(s->pg, pg);      /* structural work is left alone */
            continue;
        }
        undo_restore(pg, payload);""")

    # -- neg 4: rollback happens, but the log is still recycled out from
    #           underneath a running transaction
    d = tree("neg_still_recycles", FIXED)
    sub(d, "src/kvstore.c",
        """    if (s->in_txn) {
        ms_crash_hook("checkpoint");
        return KV_OK;
    }
    rc = wal_truncate(s->wal);""",
        """    rc = wal_truncate(s->wal);""")

    # -- neg 5: rollback happens, but the before-images are only in memory
    #           and a checkpoint is kept out of the write path instead
    d = tree("neg_memory_undo_plus_no_midtxn_checkpoint", REPO)
    sub(d, "src/kvstore.c",
        """    if (own) return kv_commit(s);
    return keep_log_bounded(s);
}

int kv_delete""",
        """    if (own) return kv_commit(s);
    return KV_OK;
}

int kv_delete""")
    sub(d, "src/kvstore.c",
        """    if (own) return kv_commit(s);
    return keep_log_bounded(s);
}

int kv_get""",
        """    if (own) return kv_commit(s);
    return KV_OK;
}

int kv_get""")
    sub(d, "src/pager.c", "#define NFRAMES 32", "#define NFRAMES 8192")

    names = sorted(os.listdir(OUT))
    print("built %d negative trees:" % len(names))
    for n in names:
        print("   ", n)


if __name__ == "__main__":
    build()
