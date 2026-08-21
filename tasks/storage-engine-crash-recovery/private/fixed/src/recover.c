/* recover.c - redo and undo recovery.
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
 */
#include <stdlib.h>
#include <string.h>
#include "internal.h"

typedef struct {
    uint64_t *ids;
    size_t    n, cap;
} txnset_t;

static int txnset_add(txnset_t *t, uint64_t id)
{
    if (t->n == t->cap) {
        size_t cap = t->cap ? t->cap * 2 : 256;
        uint64_t *p = realloc(t->ids, cap * sizeof *p);
        if (!p) return KV_ERR_IO;
        t->ids = p;
        t->cap = cap;
    }
    t->ids[t->n++] = id;
    return KV_OK;
}

/* ids are appended in log order, so the array is sorted */
static int txnset_has(const txnset_t *t, uint64_t id)
{
    size_t lo = 0, hi = t->n;
    while (lo < hi) {
        size_t mid = (lo + hi) / 2;
        if (t->ids[mid] < id) lo = mid + 1; else hi = mid;
    }
    return lo < t->n && t->ids[lo] == id;
}

static page_t *page_for(kvstore_t *s, uint32_t id, uint64_t lsn, int *apply)
{
    page_t *pg = pager_ensure(s->pg, id);
    if (!pg) return NULL;
    *apply = (PHDR(pg)->lsn < lsn);
    return pg;
}

static void stamp(kvstore_t *s, page_t *pg, uint64_t lsn)
{
    PHDR(pg)->lsn = lsn;
    pager_mark_dirty(s->pg, pg);
}

static int redo_one(kvstore_t *s, const wal_rec_t *r, const uint8_t *payload)
{
    page_t *pg, *right;
    int apply = 0;

    switch (r->type) {
    case WR_LEAF_PUT:
        pg = page_for(s, r->page, r->lsn, &apply);
        if (!pg) return KV_ERR_IO;
        if (apply) {
            if (PHDR(pg)->type != PT_LEAF) node_init(pg, PT_LEAF);
            leaf_put(pg, r->key, payload, r->vlen);
            stamp(s, pg, r->lsn);
        }
        pager_unpin(s->pg, pg);
        return KV_OK;

    case WR_LEAF_DEL:
        pg = page_for(s, r->page, r->lsn, &apply);
        if (!pg) return KV_ERR_IO;
        if (apply) {
            leaf_del(pg, r->key);
            stamp(s, pg, r->lsn);
        }
        pager_unpin(s->pg, pg);
        return KV_OK;

    case WR_LEAF_SPLIT:
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

    case WR_INT_INSERT:
        pg = page_for(s, r->page, r->lsn, &apply);
        if (!pg) return KV_ERR_IO;
        if (apply) {
            if (PHDR(pg)->type != PT_INTERNAL) node_init(pg, PT_INTERNAL);
            int_insert(pg, r->key, r->arg);
            stamp(s, pg, r->lsn);
        }
        pager_unpin(s->pg, pg);
        return KV_OK;

    case WR_NEW_ROOT:
        pg = page_for(s, r->page, r->lsn, &apply);
        if (!pg) return KV_ERR_IO;
        if (apply) {
            node_init(pg, PT_INTERNAL);
            int_set_leftmost(pg, r->aux);
            int_insert(pg, r->key, r->arg);
            stamp(s, pg, r->lsn);
        }
        pager_unpin(s->pg, pg);
        pager_meta(s->pg)->root = r->page;
        pager_meta_dirty(s->pg);
        return KV_OK;

    default:
        return KV_OK;                       /* begin / commit / checkpoint */
    }
}

/* height is bookkeeping, not data: derive it from the recovered tree */
static void recompute_height(kvstore_t *s)
{
    meta_t *m = pager_meta(s->pg);
    uint32_t id = m->root;
    uint32_t h = 1;
    for (uint32_t d = 0; d < MAX_TREE_DEPTH; d++) {
        page_t *pg = pager_get(s->pg, id);
        if (!pg) break;
        if (PHDR(pg)->type == PT_LEAF) { pager_unpin(s->pg, pg); break; }
        uint32_t next = PHDR(pg)->link;
        pager_unpin(s->pg, pg);
        if (next == INVALID_PAGE || next == id) break;
        id = next;
        h++;
    }
    m->height = h;
    pager_meta_dirty(s->pg);
}

/* Roll back every transaction that has undo records in the log but
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
{
    meta_t *m = pager_meta(s->pg);
    uint64_t ckpt = m->checkpoint_lsn;
    uint64_t max_lsn = ckpt;
    txnset_t committed = {0}, ended = {0};
    wal_rec_t r;
    uint8_t payload[PAGE_SIZE];
    int rc;

    /* pass 1: which transactions committed, and where does the log end */
    wal_read_first(s->wal);
    for (;;) {
        rc = wal_read_next(s->wal, &r, payload, sizeof payload);
        if (rc == KV_ERR_NOTFOUND || rc == KV_ERR_CORRUPT) break;
        if (rc != KV_OK) { free(committed.ids); return rc; }
        if (r.lsn > max_lsn) max_lsn = r.lsn;
        if (r.type == WR_COMMIT && txnset_add(&committed, r.txn) != KV_OK) {
            free(committed.ids);
            free(ended.ids);
            return KV_ERR_IO;
        }
        if (r.type == WR_END && txnset_add(&ended, r.txn) != KV_OK) {
            free(committed.ids);
            free(ended.ids);
            return KV_ERR_IO;
        }
    }

    /* pass 2: replay the committed work that the data file is missing */
    wal_read_first(s->wal);
    for (;;) {
        rc = wal_read_next(s->wal, &r, payload, sizeof payload);
        if (rc == KV_ERR_NOTFOUND || rc == KV_ERR_CORRUPT) break;
        if (rc != KV_OK) { free(committed.ids); free(ended.ids); return rc; }
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

    recompute_height(s);
    m = pager_meta(s->pg);
    wal_set_next_lsn(s->wal, max_lsn + 1);
    if (m->next_lsn <= max_lsn) {
        m->next_lsn = max_lsn + 1;
        pager_meta_dirty(s->pg);
    }
    rc = pager_flush_all(s->pg);
    if (rc == KV_OK) rc = pager_sync(s->pg);
    return rc;
}
