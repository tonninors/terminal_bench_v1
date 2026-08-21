#!/usr/bin/env python3
"""Rebuild private/fixed (the reference repair) from the shipped tree.

The shipped engine remembers, in memory only, what each page looked like
before the running transaction first changed it.  That is enough to roll
a transaction back while the process is alive, and nothing at all once
the process dies: the pages an unfinished transaction has already had
written back stay on disk, and there is no record anywhere of what they
used to hold.

The repair makes the undo information durable and teaches recovery to
use it:

  * a before-image goes into the log (WR_UNDO) the first time a
    transaction changes a page, before that page may be written back;
  * recovery gains a third pass that rolls back every transaction that
    has undo records but neither committed nor finished rolling back;
  * a transaction that finishes rolling back records that fact (WR_END)
    so its undo records are never applied a second time on top of later
    committed work;
  * a checkpoint may still flush an unfinished transaction's pages, but
    it may no longer recycle the log out from underneath one.

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


def sub(path, old, new, count=1):
    p = os.path.join(FIXED, path)
    s = rd(p)
    if s.count(old) < count:
        raise SystemExit("anchor not found in %s:\n%s" % (path, old[:200]))
    wr(p, s.replace(old, new, count))


def build_fixed():
    if os.path.exists(FIXED):
        shutil.rmtree(FIXED)
    shutil.copytree(REPO, FIXED,
                    ignore=lambda d, names: [n for n in names if n in SKIP])

    # ---------------------------------------------------------- internal.h
    sub("src/internal.h",
        "#define WR_CHECKPOINT  9\n",
        "#define WR_CHECKPOINT  9\n"
        "#define WR_UNDO       10   /* page as it was before this txn */\n"
        "#define WR_END        11   /* txn rolled back; do not undo again */\n")

    sub("src/internal.h",
        "    int          in_txn;\n",
        "    int          in_txn;\n"
        "    uint64_t     txn_first_lsn; /* first record of the active txn */\n"
        "    uint64_t     log_mark;      /* log size at the last checkpoint */\n")

    sub("src/internal.h",
        "int      wal_truncate(wal_t *w);\n",
        "int      wal_truncate(wal_t *w);\n"
        "int      wal_read_at(wal_t *w, uint64_t off, wal_rec_t *r,\n"
        "                     void *payload, uint32_t cap);\n"
        "uint64_t wal_read_offset(wal_t *w);\n")

    # --------------------------------------------------------------- wal.c
    sub("src/wal.c",
        """int wal_read_next(wal_t *w, wal_rec_t *r, void *payload, uint32_t cap)
{
    ssize_t n = pread(w->fd, r, sizeof *r, w->read_off);""",
        """int wal_read_next(wal_t *w, wal_rec_t *r, void *payload, uint32_t cap)
{
    uint64_t off = (uint64_t)w->read_off;
    int rc = wal_read_at(w, off, r, payload, cap);
    if (rc == KV_OK) w->read_off = (off_t)(off + r->len);
    return rc;
}

uint64_t wal_read_offset(wal_t *w) { return (uint64_t)w->read_off; }

/* Read the record at a byte offset without disturbing the iteration
 * cursor, so a pass can note where a record was and come back to it. */
int wal_read_at(wal_t *w, uint64_t at, wal_rec_t *r, void *payload,
                uint32_t cap)
{
    ssize_t n = pread(w->fd, r, sizeof *r, (off_t)at);""")

    sub("src/wal.c",
        """    uint8_t buf[sizeof(wal_rec_t) + WAL_MAX_PAYLOAD];
    n = pread(w->fd, buf, r->len, w->read_off);
    if (n != (ssize_t)r->len) return KV_ERR_CORRUPT;""",
        """    uint8_t buf[sizeof(wal_rec_t) + WAL_MAX_PAYLOAD];
    n = pread(w->fd, buf, r->len, (off_t)at);
    if (n != (ssize_t)r->len) return KV_ERR_CORRUPT;""")

    sub("src/wal.c",
        """        memcpy(payload, buf + sizeof *r, r->vlen);
    }
    w->read_off += r->len;
    return KV_OK;
}""",
        """        memcpy(payload, buf + sizeof *r, r->vlen);
    }
    return KV_OK;
}""")

    # ----------------------------------------------------------- kvstore.c
    sub("src/kvstore.c",
        """/* Keep the page as it stands, so that kv_abort can put it back.  Only the
 * first time a page is touched in a transaction matters. */
int txn_snapshot(kvstore_t *s, page_t *pg)
{
    if (!s->in_txn || !pg) return KV_OK;
    for (size_t i = 0; i < s->undo_n; i++)
        if (s->undo[i].page == pg->id) return KV_OK;""",
        """/* Keep the page as it stands, so that the transaction can be put back
 * the way it found things.  Only the first time a page is touched in a
 * transaction matters.
 *
 * The image also goes into the log.  Keeping it in memory is enough to
 * undo a transaction the process decides to abandon, and no use at all
 * for one the process does not survive: the buffer pool will write these
 * pages back long before the transaction commits, and a checkpoint
 * forced by the log budget will write them back on purpose.  Whatever is
 * needed to put them back therefore has to be on disk before they go,
 * which is the same write-ahead rule the redo records already follow. */
int txn_snapshot(kvstore_t *s, page_t *pg)
{
    if (!s->in_txn || !pg) return KV_OK;
    for (size_t i = 0; i < s->undo_n; i++)
        if (s->undo[i].page == pg->id) return KV_OK;
    {
        wal_rec_t r;
        uint64_t lsn = 0;
        int rc;
        memset(&r, 0, sizeof r);
        r.type = WR_UNDO;
        r.txn  = s->txn;
        r.page = pg->id;
        rc = wal_append(s->wal, &r, pg->buf, PAGE_SIZE, &lsn);
        if (rc != KV_OK) return rc;
        /* the page may not reach the data file before this record does;
         * the pager holds back any page whose LSN is not durable yet */
        if (PHDR(pg)->lsn < lsn) PHDR(pg)->lsn = lsn;
    }""")

    sub("src/kvstore.c",
        """    int rc = txn_begin(s);
    if (rc != KV_OK) return rc;
    s->in_txn = 1;""",
        """    int rc;
    s->txn_first_lsn = wal_next_lsn(s->wal);   /* the BEGIN record's own lsn */
    rc = txn_begin(s);
    if (rc != KV_OK) return rc;
    s->in_txn = 1;""")

    sub("src/kvstore.c",
        """    int rc = txn_commit(s);
    if (rc != KV_OK) return rc;
    s->in_txn = 0;
    txn_forget(s);
    return keep_log_bounded(s);""",
        """    int rc = txn_commit(s);
    if (rc != KV_OK) return rc;
    s->in_txn = 0;
    s->txn_first_lsn = 0;
    txn_forget(s);
    return keep_log_bounded(s);""")

    sub("src/kvstore.c",
        """    for (size_t i = s->undo_n; i-- > 0; ) {
        page_t *pg = pager_ensure(s->pg, s->undo[i].page);
        if (!pg) return KV_ERR_IO;
        memcpy(pg->buf, s->undo[i].img, PAGE_SIZE);
        pager_mark_dirty(s->pg, pg);
        pager_unpin(s->pg, pg);
    }
    s->in_txn = 0;
    txn_forget(s);
    return KV_OK;
}""",
        """    int rc;
    wal_rec_t r;
    for (size_t i = s->undo_n; i-- > 0; ) {
        page_t *pg = pager_ensure(s->pg, s->undo[i].page);
        if (!pg) return KV_ERR_IO;
        undo_restore(pg, s->undo[i].img);
        pager_mark_dirty(s->pg, pg);
        pager_unpin(s->pg, pg);
    }
    s->in_txn = 0;

    /* The rolled back pages have to be on disk before the log is allowed
     * to say this transaction needs no undoing: until they are, the only
     * thing that can repair them is the undo records still in the log. */
    rc = pager_flush_all(s->pg);
    if (rc == KV_OK) rc = pager_sync(s->pg);
    if (rc != KV_OK) return rc;

    memset(&r, 0, sizeof r);
    r.type = WR_END;
    r.txn  = s->txn;
    rc = wal_append(s->wal, &r, NULL, 0, NULL);
    if (rc == KV_OK) rc = wal_sync(s->wal);
    if (rc != KV_OK) return rc;

    s->txn_first_lsn = 0;
    txn_forget(s);
    return keep_log_bounded(s);
}""")

    # shared restore helper, used by kv_abort and by recovery
    sub("src/kvstore.c",
        """static void txn_forget(kvstore_t *s)""",
        """/* Put a page back the way the image found it.
 *
 * The meta page is not restored wholesale: the tree fields belong to the
 * transaction and have to go back, but how far the log has been absorbed
 * and which sequence numbers have been handed out are facts about the
 * log, not about the transaction, and undoing those would replay work
 * that is already durable. */
void undo_restore(page_t *pg, const uint8_t *img)
{
    if (pg->id == META_PAGE) {
        meta_t *cur = (meta_t *)pg->buf;
        const meta_t *was = (const meta_t *)img;
        cur->hdr = was->hdr;
        cur->num_pages = was->num_pages;
        cur->root = was->root;
        cur->height = was->height;
    } else {
        memcpy(pg->buf, img, PAGE_SIZE);
    }
}

static void txn_forget(kvstore_t *s)""")

    sub("src/internal.h",
        "int txn_snapshot(kvstore_t *s, page_t *pg);",
        "int txn_snapshot(kvstore_t *s, page_t *pg);\n"
        "void undo_restore(page_t *pg, const uint8_t *img);")

    # the budget must not re-fire on every operation once the log is being
    # held open by a long transaction
    sub("src/kvstore.c",
        """static int keep_log_bounded(kvstore_t *s)
{
    if (wal_bytes(s->wal) < MS_LOG_LIMIT) return KV_OK;
    return kv_checkpoint(s);
}""",
        """static int keep_log_bounded(kvstore_t *s)
{
    if (wal_bytes(s->wal) < s->log_mark + MS_LOG_LIMIT) return KV_OK;
    int rc = kv_checkpoint(s);
    if (rc == KV_OK) s->log_mark = wal_bytes(s->wal);
    return rc;
}""")

    sub("src/kvstore.c",
        """    /* Every page is on disk and the meta page records how far the log has
     * been absorbed, so the log can be recycled: nothing before this
     * point will ever be replayed again. */
    rc = wal_truncate(s->wal);
    if (rc == KV_OK) ms_crash_hook("checkpoint");
    return rc;""",
        """    /* Every page is on disk and the meta page records how far the log has
     * been absorbed, so nothing before this point will ever be replayed
     * again - but replay is not the only thing the log is for.  A
     * transaction still running has pages on disk that this checkpoint
     * has just published, and the only description of what they held
     * before it touched them is in the records this would throw away.
     * The log may not be recycled out from underneath it. */
    if (s->in_txn) {
        ms_crash_hook("checkpoint");
        return KV_OK;
    }
    rc = wal_truncate(s->wal);
    if (rc != KV_OK) return rc;
    s->log_mark = 0;
    ms_crash_hook("checkpoint");
    return rc;""")

    # ----------------------------------------------------------- recover.c
    sub("src/recover.c",
        """/* recover.c - redo recovery.
 *
 * Two passes over the log.  The first pass finds the transactions that
 * reached a commit record and where the log stops being readable (a torn
 * tail left by the crash).  The second pass replays the records of those
 * transactions against the data file, skipping any page whose stored LSN
 * shows the change already reached disk.  Replaying an already recovered
 * database must therefore change nothing.
 */""",
        """/* recover.c - redo and undo recovery.
 *
 * Three passes over the log.  The first finds the transactions that
 * reached a commit record, the ones that finished rolling back, and
 * where the log stops being readable (the torn tail a crash leaves).
 * The second replays the records of committed transactions against the
 * data file, skipping any page whose stored LSN shows the change already
 * reached disk.  The third rolls back what is left: a transaction that
 * wrote undo records but neither committed nor finished rolling back was
 * interrupted, and the pages it had already had written back have to go
 * back to what they held before it touched them.
 *
 * Both replay and rollback are repeatable.  Replay is gated on the page
 * LSN; rollback puts back a fixed image, so doing it twice lands in the
 * same place as doing it once, and being interrupted part way through
 * simply means the next attempt starts again from the same records.
 */""")

    sub("src/recover.c",
        """int recover_redo(kvstore_t *s)
{""",
        """/* Roll back every transaction that has undo records in the log but
 * neither a commit record nor an end-of-rollback record.  The images are
 * applied newest first, so a page changed several times ends up at the
 * oldest image kept for it. */
static int recover_undo(kvstore_t *s, const txnset_t *committed,
                        const txnset_t *ended)
{
    uint64_t *offs = NULL;
    size_t n = 0, cap = 0;
    wal_rec_t r;
    uint8_t payload[PAGE_SIZE];
    txnset_t losers = {0};
    int rc;

    wal_read_first(s->wal);
    for (;;) {
        uint64_t off = wal_read_offset(s->wal);
        rc = wal_read_next(s->wal, &r, payload, sizeof payload);
        if (rc == KV_ERR_NOTFOUND || rc == KV_ERR_CORRUPT) break;
        if (rc != KV_OK) goto io;
        if (r.type != WR_UNDO) continue;
        if (txnset_has(committed, r.txn) || txnset_has(ended, r.txn)) continue;
        if (n == cap) {
            size_t c = cap ? cap * 2 : 256;
            uint64_t *p = realloc(offs, c * sizeof *p);
            if (!p) goto io;
            offs = p;
            cap = c;
        }
        offs[n++] = off;
        if (!txnset_has(&losers, r.txn) &&
            txnset_add(&losers, r.txn) != KV_OK) goto io;
    }
    if (n == 0) { free(offs); free(losers.ids); return KV_OK; }

    for (size_t i = n; i-- > 0; ) {
        rc = wal_read_at(s->wal, offs[i], &r, payload, sizeof payload);
        if (rc != KV_OK) goto io;
        if (r.vlen != PAGE_SIZE) goto io;
        page_t *pg = pager_ensure(s->pg, r.page);
        if (!pg) goto io;
        undo_restore(pg, payload);
        pager_mark_dirty(s->pg, pg);
        pager_unpin(s->pg, pg);
    }
    free(offs);
    offs = NULL;

    /* the rolled back pages go to disk before anything records that the
     * rollback is done, so an interrupted rollback is simply run again */
    rc = pager_flush_all(s->pg);
    if (rc == KV_OK) rc = pager_sync(s->pg);
    if (rc != KV_OK) { free(losers.ids); return rc; }

    for (size_t i = 0; i < losers.n; i++) {
        memset(&r, 0, sizeof r);
        r.type = WR_END;
        r.txn  = losers.ids[i];
        rc = wal_append(s->wal, &r, NULL, 0, NULL);
        if (rc != KV_OK) { free(losers.ids); return rc; }
    }
    rc = wal_sync(s->wal);
    free(losers.ids);
    return rc;

io:
    free(offs);
    free(losers.ids);
    return KV_ERR_IO;
}

int recover_redo(kvstore_t *s)
{""")

    sub("src/recover.c",
        """    txnset_t committed = {0};
    wal_rec_t r;""",
        """    txnset_t committed = {0}, ended = {0};
    wal_rec_t r;""")

    sub("src/recover.c",
        """        if (r.type == WR_COMMIT && txnset_add(&committed, r.txn) != KV_OK) {
            free(committed.ids);
            return KV_ERR_IO;
        }
    }""",
        """        if (r.type == WR_COMMIT && txnset_add(&committed, r.txn) != KV_OK) {
            free(committed.ids);
            free(ended.ids);
            return KV_ERR_IO;
        }
        if (r.type == WR_END && txnset_add(&ended, r.txn) != KV_OK) {
            free(committed.ids);
            free(ended.ids);
            return KV_ERR_IO;
        }
    }""")

    sub("src/recover.c",
        """        if (rc != KV_OK) { free(committed.ids); return rc; }
        if (r.lsn <= ckpt) continue;
        if (!txnset_has(&committed, r.txn)) continue;
        rc = redo_one(s, &r, payload);
        if (rc != KV_OK) { free(committed.ids); return rc; }
    }
    free(committed.ids);
""",
        """        if (rc != KV_OK) { free(committed.ids); free(ended.ids); return rc; }
        if (r.lsn <= ckpt) continue;
        if (!txnset_has(&committed, r.txn)) continue;
        rc = redo_one(s, &r, payload);
        if (rc != KV_OK) { free(committed.ids); free(ended.ids); return rc; }
    }

    /* Sequence numbers have to run past everything already in the log
     * before the next pass is allowed to append to it. */
    wal_set_next_lsn(s->wal, max_lsn + 1);

    /* pass 3: put back what an interrupted transaction had already
     * written.  Undo records are not gated on the checkpoint: a
     * checkpoint taken inside a transaction publishes exactly the pages
     * this pass exists to repair. */
    rc = recover_undo(s, &committed, &ended);
    free(committed.ids);
    free(ended.ids);
    if (rc != KV_OK) return rc;
""")

    # waldump should decode the new record types
    sub("tools/kvcli.c",
        '    case WR_CHECKPOINT: return "CHECKPOINT";\n',
        '    case WR_CHECKPOINT: return "CHECKPOINT";\n'
        '    case WR_UNDO:       return "UNDO";\n'
        '    case WR_END:        return "END";\n')


def make_patch():
    r = subprocess.run(["diff", "-ru", "repo", "private/fixed",
                        "-x", "build", "-x", "testwork"],
                       cwd=TASK, capture_output=True, text=True)
    wr(PATCH, r.stdout)


if __name__ == "__main__":
    build_fixed()
    make_patch()
    print("rebuilt private/fixed and private/reference_fix.patch")
